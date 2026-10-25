# Copyright (c) 2023 Carter Lasalle
"""Small facts chat.db records that reads did not show, and Apple's Send Later.

Attachments: whether a missing file is simply not downloaded from iCloud, and
Genmoji descriptions. Messages: subject lines, send effects, voice memos that
were played, time-sensitive and quietly-delivered flags, Contact Key
Verification, and which of the user's own addresses a message went through.
Scheduling: messages the user scheduled from Messages.app itself.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from mac_messages_mcp.messages import get_recent_messages, search_attachments
from mac_messages_mcp.untrusted import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

from .chatdb import apple_ns, attachment_only_attributed_body

ALICE = "+15555550100"


def _inner(fenced: str) -> str:
    assert fenced.startswith(UNTRUSTED_OPEN) and fenced.endswith(UNTRUSTED_CLOSE)
    return (
        fenced[len(UNTRUSTED_OPEN) : -len(UNTRUSTED_CLOSE)].strip().replace("\\n", "\n")
    )


# ---------------------------------------------------------------------------
# #7 attachments: not-downloaded vs missing, Genmoji
# ---------------------------------------------------------------------------


def test_not_downloaded_attachment_is_distinguished_from_missing(chat_db, tmp_path):
    alice = chat_db.add_handle(ALICE)
    m1 = chat_db.add_message("cloud", handle_id=alice)
    a1 = chat_db.add_attachment(
        m1, str(tmp_path / "IMG_1.heic"), "image/heic", transfer_state=0
    )
    m2 = chat_db.add_message("gone", handle_id=alice)
    a2 = chat_db.add_attachment(
        m2, str(tmp_path / "IMG_2.heic"), "image/heic", transfer_state=5
    )

    out = _inner(search_attachments())
    assert f"id={a1}" in out and "[not downloaded from iCloud]" in out
    assert f"id={a2}" in out and "[missing on disk]" in out
    data = json.loads(_inner(search_attachments(format="json")))
    by_id = {a["id"]: a for a in data["attachments"]}
    assert by_id[a1]["downloaded"] is False and by_id[a2]["downloaded"] is True


def test_genmoji_description_is_shown(chat_db, tmp_path):
    alice = chat_db.add_handle(ALICE)
    msg = chat_db.add_message(
        None, handle_id=alice, attributedBody=attachment_only_attributed_body()
    )
    chat_db.add_attachment(
        msg,
        str(tmp_path / "sticker.png"),
        "image/png",
        emoji_image_short_description="a cat wearing a top hat",
    )
    out = _inner(get_recent_messages(hours=1))
    assert "genmoji: a cat wearing a top hat" in out
    data = json.loads(_inner(get_recent_messages(hours=1, format="json")))
    assert data["messages"][0]["attachments"][0]["genmoji"] == "a cat wearing a top hat"


# ---------------------------------------------------------------------------
# #7 message tags
# ---------------------------------------------------------------------------


def test_subject_effect_played_and_flags(chat_db, tmp_path):
    alice = chat_db.add_handle(ALICE)
    chat_db.add_message("body text", handle_id=alice, subject="Re: dinner")
    chat_db.add_message(
        "BOOM",
        handle_id=alice,
        expressive_send_style_id="com.apple.MobileSMS.expressivesend.impact",
    )
    voice = chat_db.add_message(
        None,
        handle_id=alice,
        attributedBody=attachment_only_attributed_body(),
        is_audio_message=1,
        date_played=apple_ns(),
    )
    chat_db.add_attachment(voice, str(tmp_path / "Audio Message.caf"), "audio/x-caf")
    chat_db.add_message("urgent", handle_id=alice, is_time_sensitive=1)
    chat_db.add_message("quiet", handle_id=alice, is_from_me=1, was_delivered_quietly=1)
    chat_db.add_message(
        "verified",
        handle_id=alice,
        is_kt_verified=1,
        account="E:lane@example.com",
        destination_caller_id="+15555550000",
    )

    out = _inner(get_recent_messages(hours=1))
    assert "[subject: Re: dinner]: body text" in out
    assert "[effect: impact]: BOOM" in out
    assert "[audio message] [played]" in out
    assert "[time sensitive]: urgent" in out
    assert "[delivered quietly]: quiet" in out

    data = json.loads(_inner(get_recent_messages(hours=1, format="json")))
    by_body = {m["body"]: m for m in data["messages"]}
    assert by_body["body text"]["subject"] == "Re: dinner"
    assert by_body["BOOM"]["effect"] == "impact"
    assert by_body["urgent"]["time_sensitive"] is True
    assert by_body["quiet"]["delivered_quietly"] is True
    assert by_body["verified"]["kt_verified"] is True
    assert by_body["verified"]["via_account"] == "E:lane@example.com"
    assert by_body["verified"]["to_my_address"] == "+15555550000"
    assert by_body["[attachment]"]["played_at"] is not None


# ---------------------------------------------------------------------------
# #6 Apple Send Later
# ---------------------------------------------------------------------------


def test_apple_scheduled_sends_appear_in_the_scheduled_list(
    chat_db, monkeypatch, tmp_path
):
    from mac_messages_mcp import server

    alice = chat_db.add_handle(ALICE)
    chat = chat_db.add_chat(ALICE, handles=(alice,))
    later = datetime.now(timezone.utc) + timedelta(hours=5)
    pending = chat_db.add_message(
        "happy birthday!",
        handle_id=alice,
        is_from_me=1,
        is_sent=0,
        chat_id=chat,
        date=apple_ns(later),
        schedule_type=2,
        schedule_state=1,
    )
    chat_db.add_message(
        "already went",
        handle_id=alice,
        is_from_me=1,
        is_sent=1,
        chat_id=chat,
        schedule_type=2,
        schedule_state=2,
    )
    chat_db.add_message(
        "ordinary", handle_id=alice, is_from_me=1, is_sent=1, chat_id=chat
    )

    out = _inner(server.tool_list_scheduled_messages())
    assert "Scheduled in Messages.app (Send Later):" in out
    assert f"apple-{pending}" in out and "happy birthday!" in out
    assert "already went" not in out and "ordinary" not in out
    assert ALICE in out


def test_no_apple_scheduled_sends_means_no_section(chat_db):
    from mac_messages_mcp import server

    out = _inner(server.tool_list_scheduled_messages())
    assert "Send Later" not in out
    assert "No messages are scheduled" in out
