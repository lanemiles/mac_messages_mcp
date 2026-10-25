# Copyright (c) 2023 Carter Lasalle
"""Every read path's SQL, run end-to-end against the production schema.

The rest of the suite mocks ``query_messages_db``, which proves the Python
around a query but never the query. These tests use the schema-faithful
``chat_db`` / ``address_book`` fixtures (DDL dumped from a real Mac, see
``tests/fixtures/``), so a misspelled column, a wrong join, or an unsatisfied
NOT NULL fails here instead of on a user's machine.
"""

from __future__ import annotations

import io
import time
from datetime import datetime, timezone

import pytest

from mac_messages_mcp import messages, read
from mac_messages_mcp.content import search_attachment_contents
from mac_messages_mcp.messages import (
    check_messages_db_access,
    find_handles_by_phone,
    fuzzy_search_messages,
    get_attachment,
    get_recent_messages,
    list_conversations,
    search_attachments,
    wait_for_new_messages,
)
from mac_messages_mcp.send import _check_imessage_availability, _verify_send_in_db
from mac_messages_mcp.untrusted import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

from .chatdb import (
    ATTACHMENT_PLACEHOLDER,
    SAMPLE_BODY_TEXT,
    apple_ns,
    attachment_only_attributed_body,
    sample_attributed_body,
)

ALICE_PHONE = "+15555550100"
ALICE_EMAIL = "alice@example.com"
BOB_PHONE = "+15555550199"


def _text(fenced: str) -> str:
    """Unwrap the untrusted fence and restore structural newlines."""
    assert fenced.startswith(UNTRUSTED_OPEN) and fenced.endswith(UNTRUSTED_CLOSE)
    inner = fenced[len(UNTRUSTED_OPEN) : -len(UNTRUSTED_CLOSE)].strip()
    assert "Error" not in inner.split("\\n")[0][:40], inner
    return inner.replace("\\n", "\n")


@pytest.fixture
def fake_clock(monkeypatch):
    state = {"now": 0.0, "sleeps": []}
    monkeypatch.setattr(read.time, "monotonic", lambda: state["now"])

    def sleep(seconds):
        state["sleeps"].append(seconds)
        state["now"] += seconds

    monkeypatch.setattr(read.time, "sleep", sleep)
    return state


# ---------------------------------------------------------------------------
# Names: the AddressBook SQL joined to the handle table
# ---------------------------------------------------------------------------


def test_sender_names_resolve_through_real_addressbook_sql(chat_db, address_book):
    address_book.add_contact(
        "Alice", "Example", phones=("(555) 555-0100",), emails=(ALICE_EMAIL,)
    )
    phone = chat_db.add_handle(ALICE_PHONE)
    email = chat_db.add_handle(ALICE_EMAIL)
    chat_db.add_message("via phone", handle_id=phone)
    chat_db.add_message("via email", handle_id=email)
    chat_db.add_message("mine", handle_id=phone, is_from_me=1)

    out = _text(get_recent_messages(hours=1))

    assert out.count("Alice Example") == 2, out
    assert "You [iMessage]: mine" in out


def test_contact_filter_by_name_covers_every_handle_of_one_person(
    chat_db, address_book
):
    address_book.add_contact(
        "Alice", "Example", phones=("555-555-0100",), emails=(ALICE_EMAIL,)
    )
    address_book.add_contact("Bob", "Builder", phones=(BOB_PHONE,))
    phone = chat_db.add_handle(ALICE_PHONE)
    email = chat_db.add_handle(ALICE_EMAIL)
    bob = chat_db.add_handle(BOB_PHONE)
    chat_db.add_message("alice by phone", handle_id=phone)
    chat_db.add_message("alice by email", handle_id=email)
    chat_db.add_message("bob here", handle_id=bob)

    out = _text(get_recent_messages(hours=1, contact="Alice Example"))

    assert "alice by phone" in out and "alice by email" in out
    assert "bob here" not in out


def test_contact_filter_by_national_phone_finds_e164_handle(chat_db):
    alice = chat_db.add_handle(ALICE_PHONE)
    chat_db.add_message("hi", handle_id=alice)

    assert find_handles_by_phone("(555) 555-0100") == [alice]
    assert "hi" in _text(get_recent_messages(hours=1, contact="555-555-0100"))


# ---------------------------------------------------------------------------
# Group chats: chat / chat_message_join / chat_handle_join
# ---------------------------------------------------------------------------


@pytest.fixture
def family(chat_db):
    alice = chat_db.add_handle(ALICE_PHONE)
    bob = chat_db.add_handle(BOB_PHONE)
    group = chat_db.add_chat(
        "chat721054478304420871",
        style=43,
        display_name="Family",
        handles=(alice, bob),
    )
    direct = chat_db.add_chat(ALICE_PHONE, style=45, handles=(alice,))
    chat_db.add_message("group hello", handle_id=alice, chat_id=group)
    chat_db.add_message("group reply", handle_id=0, is_from_me=1, chat_id=group)
    chat_db.add_message("direct hello", handle_id=alice, chat_id=direct)
    return {"alice": alice, "bob": bob, "group": group, "direct": direct}


@pytest.mark.parametrize(
    "chat_id", ["chat721054478304420871", "iMessage;+;chat721054478304420871"]
)
def test_group_read_by_either_chat_id_spelling(chat_db, family, chat_id):
    out = _text(get_recent_messages(hours=1, chat_id=chat_id))
    assert "[Family]" in out
    assert "group hello" in out and "group reply" in out
    assert "direct hello" not in out


def test_named_group_appears_in_tool_get_chats_and_conversation_list(chat_db, family):
    from mac_messages_mcp.server import tool_get_chats

    chats = _text(tool_get_chats())
    assert "Family (ID: chat721054478304420871)" in chats

    biz = chat_db.add_chat("urn:biz:0b1c2d3e-example", style=45)
    chat_db.add_message("your order shipped", handle_id=0, chat_id=biz)

    convos = _text(list_conversations())
    assert "[group] Family (chat ID: chat721054478304420871)" in convos
    assert "2 message(s), 1 unread" in convos
    # Style 45 is the ordinary 1:1 style; only a urn:biz identifier is business.
    assert f"[direct] {ALICE_PHONE} (chat ID: {ALICE_PHONE})" in convos
    assert "[business] urn:biz:0b1c2d3e-example" in convos


def test_wait_scoped_to_chat_id_probes_only_that_chat(chat_db, family, fake_clock):
    cursor = chat_db.conn.execute("SELECT MAX(ROWID) FROM message").fetchone()[0]
    # Traffic in the direct thread must not wake a wait on the group.
    chat_db.add_message(
        "more direct", handle_id=family["alice"], chat_id=family["direct"]
    )

    out = _text(
        wait_for_new_messages(
            since_rowid=cursor,
            chat_id="chat721054478304420871",
            timeout_seconds=2,
            poll_interval=1,
        )
    )
    assert "No new messages arrived within 2 seconds" in out
    assert fake_clock["sleeps"]

    fake_clock["sleeps"].clear()
    new_id = chat_db.add_message(
        "group news", handle_id=family["bob"], chat_id=family["group"]
    )
    out = _text(
        wait_for_new_messages(
            since_rowid=cursor, chat_id="chat721054478304420871", timeout_seconds=2
        )
    )
    assert f"#{new_id} " in out and "group news" in out
    assert "more direct" not in out
    assert f"since_rowid={new_id}" in out
    assert not fake_clock["sleeps"]


def test_fuzzy_search_scoped_by_chat_and_by_contact(chat_db, family):
    by_chat = _text(
        fuzzy_search_messages("hello", hours=1, chat_id="chat721054478304420871")
    )
    assert "group hello" in by_chat and "direct hello" not in by_chat

    by_contact = _text(fuzzy_search_messages("hello", hours=1, contact=ALICE_PHONE))
    assert "group hello" in by_contact and "direct hello" in by_contact


# ---------------------------------------------------------------------------
# Row metadata and filters on the real column set
# ---------------------------------------------------------------------------


def test_tapback_reply_and_unread_tags_render_from_real_columns(chat_db):
    alice = chat_db.add_handle(ALICE_PHONE)
    chat_db.add_message("original", handle_id=alice, is_read=1, guid="orig-guid")
    chat_db.add_message(
        None,
        handle_id=alice,
        associated_message_type=2000,
        associated_message_guid="p:0/orig-guid",
    )
    chat_db.add_message(
        "threaded answer", handle_id=alice, thread_originator_guid="orig-guid"
    )
    chat_db.add_message("not yet seen", handle_id=alice, is_read=0)

    out = _text(get_recent_messages(hours=1))
    assert '[tapback: loved → "original"]: (reaction)' in out
    assert '[reply to: "original"]: threaded answer' in out
    assert "[unread]: not yet seen" in out

    unread = _text(get_recent_messages(hours=1, unread_only=True))
    assert "not yet seen" in unread
    assert "]: original" not in unread, "the read message itself is excluded"


def test_date_range_filters_use_apple_epoch_correctly(chat_db):
    alice = chat_db.add_handle(ALICE_PHONE)
    jan15 = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
    jan16 = datetime(2026, 1, 16, 12, tzinfo=timezone.utc)
    chat_db.add_message("on the 15th", handle_id=alice, date=apple_ns(jan15))
    chat_db.add_message("on the 16th", handle_id=alice, date=apple_ns(jan16))

    one_day = _text(get_recent_messages(start_date="2026-01-15", end_date="2026-01-15"))
    assert "on the 15th" in one_day and "on the 16th" not in one_day

    open_ended = _text(get_recent_messages(start_date="2026-01-16"))
    assert "on the 16th" in open_ended and "on the 15th" not in open_ended


# ---------------------------------------------------------------------------
# Attachments: attachment / message_attachment_join, plus the files on disk
# ---------------------------------------------------------------------------


def _png_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def test_attachment_search_and_fetch(chat_db, tmp_path):
    alice = chat_db.add_handle(ALICE_PHONE)
    picture = tmp_path / "IMG_0001.png"
    picture.write_bytes(_png_bytes())
    msg = chat_db.add_message("look", handle_id=alice)
    att = chat_db.add_attachment(msg, str(picture), "image/png")
    gone = chat_db.add_attachment(
        chat_db.add_message("old", handle_id=alice),
        str(tmp_path / "missing.jpg"),
        "image/jpeg",
    )
    chat_db.add_attachment(
        chat_db.add_message("sticker", handle_id=alice),
        str(picture),
        "image/png",
        is_sticker=1,
    )

    listing = _text(search_attachments(mime_type="image/"))
    assert f"id={att} msg={msg}" in listing
    assert f"id={gone}" in listing and "[missing on disk]" in listing
    assert listing.count("- id=") == 2, "stickers must be filtered out"

    # Inline fetch: the message line in the read names the attachment id too.
    read = _text(get_recent_messages(hours=1))
    assert f"[attachments: #{att} image/png (IMG_0001.png)]" in read

    fetched = get_attachment(att)
    assert isinstance(fetched, list) and len(fetched) == 2
    assert "IMG_0001.png" in str(fetched[0])
    assert fetched[1].data == picture.read_bytes()


def test_attachment_content_search_reads_text_files(chat_db, tmp_path):
    alice = chat_db.add_handle(ALICE_PHONE)
    note = tmp_path / "notes.txt"
    note.write_text("Reminder: the lease renewal is due Friday.\n")
    msg = chat_db.add_message("see attached", handle_id=alice)
    att = chat_db.add_attachment(msg, str(note), "text/plain")

    out = _text(search_attachment_contents("lease renewal"))
    assert f"#{att} notes.txt [text/plain]" in out
    assert "lease renewal is due" in out


# ---------------------------------------------------------------------------
# Send-side reads: availability heuristic and post-send verification
# ---------------------------------------------------------------------------


def test_imessage_availability_reads_handle_service_and_error(chat_db):
    alice = chat_db.add_handle(ALICE_PHONE, service="iMessage")
    bob = chat_db.add_handle(BOB_PHONE, service="SMS")
    chat_db.add_message("ok", handle_id=alice, error=0)
    chat_db.add_message("sms only", handle_id=bob, service="SMS")

    assert _check_imessage_availability(ALICE_PHONE) is True
    assert _check_imessage_availability("(555) 555-0100") is True
    assert _check_imessage_availability(BOB_PHONE) is False


def test_send_verification_finds_this_sends_row(chat_db):
    alice = chat_db.add_handle(ALICE_PHONE)
    sent_after = time.time() - 5
    chat_db.add_message("earlier", handle_id=alice, is_from_me=1, is_sent=1)
    chat_db.add_message(
        "the one we sent",
        handle_id=alice,
        is_from_me=1,
        is_sent=1,
        is_delivered=1,
        error=0,
    )

    row = _verify_send_in_db(ALICE_PHONE, sent_after, "the one we sent", timeout_s=1)

    assert row is not None
    assert row["is_sent"] == 1 and row["is_delivered"] == 1
    assert not row["send_error"]
    assert row["text"] == "the one we sent"


# ---------------------------------------------------------------------------
# attributedBody: a real typedstream blob, as macOS 27 writes it
# ---------------------------------------------------------------------------


def test_real_attributed_body_decodes():
    decoded = messages.extract_body_from_attributed(sample_attributed_body())
    assert decoded == ATTACHMENT_PLACEHOLDER + SAMPLE_BODY_TEXT


def test_text_with_attachment_renders_words_without_placeholder_glyph(
    chat_db, tmp_path
):
    # Messages leaves `text` NULL and stores the body only in attributedBody,
    # with U+FFFC marking where the photo sits. The `text` column, when Apple
    # does fill it, omits that character; our rendering must too.
    alice = chat_db.add_handle(ALICE_PHONE)
    msg = chat_db.add_message(
        None, handle_id=alice, attributedBody=sample_attributed_body()
    )
    att = chat_db.add_attachment(msg, str(tmp_path / "IMG_1.jpeg"), "image/jpeg")

    out = _text(get_recent_messages(hours=1))
    assert SAMPLE_BODY_TEXT in out
    assert ATTACHMENT_PLACEHOLDER not in out
    assert f"[attachments: #{att} image/jpeg" in out

    found = _text(fuzzy_search_messages("fixture probe", hours=1))
    assert SAMPLE_BODY_TEXT in found
    assert ATTACHMENT_PLACEHOLDER not in found


def test_attachment_only_message_renders_as_attachment_marker(chat_db, tmp_path):
    alice = chat_db.add_handle(ALICE_PHONE)
    msg = chat_db.add_message(
        None, handle_id=alice, attributedBody=attachment_only_attributed_body()
    )
    att = chat_db.add_attachment(msg, str(tmp_path / "IMG_2.heic"), "image/heic")

    out = _text(get_recent_messages(hours=1))
    assert f"#{msg} " in out, "an attachment-only message must not be dropped"
    assert ": [attachment] " in out
    assert ATTACHMENT_PLACEHOLDER not in out
    assert f"[attachments: #{att} image/heic" in out


def test_send_verification_matches_a_text_plus_attachment_send(chat_db):
    # The row for a text+photo send has text NULL and the words behind a U+FFFC
    # in attributedBody. Verification must still correlate on the words, or it
    # falls back to "newest row" and can report another send's outcome.
    alice = chat_db.add_handle(ALICE_PHONE)
    sent_after = time.time() - 5
    ours = chat_db.add_message(
        None,
        handle_id=alice,
        is_from_me=1,
        is_sent=1,
        attributedBody=sample_attributed_body(),
    )
    chat_db.add_message(
        "a later, unrelated send", handle_id=alice, is_from_me=1, is_sent=1, error=22
    )

    row = _verify_send_in_db(ALICE_PHONE, sent_after, SAMPLE_BODY_TEXT, timeout_s=1)

    assert row is not None
    assert row["guid"] == f"msg-{ours:04d}", row
    assert not row["send_error"]


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_db_access_check_sees_the_required_tables(chat_db):
    report = check_messages_db_access()
    assert "Required tables (message, handle) are present" in report
    assert "ERROR" not in report
