# Copyright (c) 2023 Carter Lasalle
"""Message details that chat.db records but reads used to drop or flatten.

A tapback said "(reaction)" without saying to what; a reply said "[reply]"
without saying to what; edited, unsent, audio, link-preview and group-event
rows were either unlabelled or skipped entirely because they carry no body;
and delivery/read receipts on sent messages were never shown.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from mac_messages_mcp.messages import get_recent_messages
from mac_messages_mcp.untrusted import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

from .chatdb import apple_ns, attachment_only_attributed_body

ALICE = "+15555550100"


def _inner(fenced: str) -> str:
    assert fenced.startswith(UNTRUSTED_OPEN) and fenced.endswith(UNTRUSTED_CLOSE)
    return (
        fenced[len(UNTRUSTED_OPEN) : -len(UNTRUSTED_CLOSE)].strip().replace("\\n", "\n")
    )


def _json(fenced: str):
    return json.loads(_inner(fenced))


def _lines(fenced: str) -> list[str]:
    return [l for l in _inner(fenced).splitlines() if l.startswith("[")]


@pytest.fixture
def alice(chat_db):
    return chat_db.add_handle(ALICE)


def test_tapback_quotes_the_message_it_reacted_to(chat_db, alice):
    chat_db.add_message(
        "Dinner at seven? I booked the corner table for us all",
        handle_id=alice,
        guid="orig-1",
    )
    chat_db.add_message(
        None,
        handle_id=alice,
        associated_message_type=2000,
        associated_message_guid="p:0/orig-1",
    )
    chat_db.add_message(
        None,
        handle_id=alice,
        associated_message_type=2006,
        associated_message_guid="bp:orig-1",
        associated_message_emoji="🔥",
    )

    long_body = (
        "This is a much longer message whose quote has to be cut off somewhere sensible"
    )
    chat_db.add_message(long_body, handle_id=alice, guid="orig-2")
    chat_db.add_message(
        None,
        handle_id=alice,
        associated_message_type=2001,
        associated_message_guid="p:0/orig-2",
    )

    text = _inner(get_recent_messages(hours=1))
    assert (
        '[tapback: loved → "Dinner at seven? I booked the corner table for us all"]'
        in text
    )
    assert '[tapback: 🔥 → "Dinner at seven?' in text
    # Long targets are quoted to 60 characters with an ellipsis.
    assert (
        '[tapback: liked → "This is a much longer message whose quote has to be cut off…"]'
        in text
    )

    data = _json(get_recent_messages(hours=1, format="json"))
    loved = [
        m
        for m in data["messages"]
        if m.get("tapback") and m["tapback"]["kind"] == "loved"
    ][0]
    assert loved["tapback"]["target"]["body"].startswith("Dinner at seven?")
    assert loved["tapback"]["target"]["guid"] == "orig-1"
    fire = [
        m
        for m in data["messages"]
        if m.get("tapback") and m["tapback"].get("emoji") == "🔥"
    ][0]
    assert fire["tapback"]["kind"] == "emoji"


def test_reply_quotes_the_original(chat_db, alice):
    chat_db.add_message("Which train are you on?", handle_id=alice, guid="q-1")
    chat_db.add_message(
        "The 6:40", handle_id=alice, is_from_me=1, thread_originator_guid="q-1"
    )
    text = _inner(get_recent_messages(hours=1))
    assert '[reply to: "Which train are you on?"]: The 6:40' in text
    data = _json(get_recent_messages(hours=1, format="json"))
    reply = data["messages"][0]
    assert reply["reply_to"] == {"guid": "q-1", "body": "Which train are you on?"}


def test_edited_and_unsent_messages_are_visible(chat_db, alice):
    now = datetime.now(timezone.utc)
    chat_db.add_message("corrected wording", handle_id=alice, date_edited=apple_ns(now))
    chat_db.add_message(None, handle_id=alice, date_retracted=apple_ns(now))
    text = _inner(get_recent_messages(hours=1))
    assert "[edited]: corrected wording" in text
    assert (
        "[unsent]: (unsent message)" in text
    ), "a retracted row has no body but must not vanish"
    data = _json(get_recent_messages(hours=1, format="json"))
    unsent = [m for m in data["messages"] if m["body"] == "(unsent message)"][0]
    assert unsent["unsent_at"] is not None
    edited = [m for m in data["messages"] if m["body"] == "corrected wording"][0]
    assert edited["edited_at"] is not None


def test_audio_message_and_link_preview_are_labelled(chat_db, alice, tmp_path):
    voice = chat_db.add_message(
        None,
        handle_id=alice,
        attributedBody=attachment_only_attributed_body(),
        is_audio_message=1,
    )
    chat_db.add_attachment(voice, str(tmp_path / "Audio Message.caf"), "audio/x-caf")
    chat_db.add_message(
        "https://example.com/story",
        handle_id=alice,
        balloon_bundle_id="com.apple.messages.URLBalloonProvider",
    )
    chat_db.add_message(
        None,
        handle_id=alice,
        balloon_bundle_id="com.apple.PassbookUIService.PeerPaymentMessagesExtension",
        attributedBody=attachment_only_attributed_body(),
    )

    text = _inner(get_recent_messages(hours=1))
    assert "[audio message]: [attachment]" in text
    assert "[link preview]: https://example.com/story" in text
    assert "[app: PeerPaymentMessagesExtension]" in text

    data = _json(get_recent_messages(hours=1, format="json"))
    kinds = {m["body"]: m for m in data["messages"]}
    assert kinds["https://example.com/story"]["link_preview"] is True
    assert kinds["[attachment]"]["is_audio"] is True or any(
        m["is_audio"] for m in data["messages"]
    )


def test_group_events_render_as_events(chat_db, address_book):
    address_book.add_contact("Alice", "Example", phones=(ALICE,))
    address_book.add_contact("Bob", "Builder", phones=("+15555550199",))
    alice = chat_db.add_handle(ALICE)
    bob = chat_db.add_handle("+15555550199")
    group = chat_db.add_chat(
        "chat1", style=43, display_name="Trip", handles=(alice, bob)
    )
    chat_db.add_message(
        None,
        handle_id=alice,
        chat_id=group,
        item_type=2,
        group_title="Trip 2026",
        group_action_type=0,
    )
    chat_db.add_message(
        None,
        handle_id=alice,
        chat_id=group,
        item_type=1,
        group_action_type=0,
        other_handle=bob,
    )
    chat_db.add_message(None, handle_id=bob, chat_id=group, item_type=3)

    text = _inner(get_recent_messages(hours=1))
    assert '[event]: Alice Example named the group "Trip 2026"' in text
    assert "[event]: Alice Example added Bob Builder" in text
    assert "[event]: Bob Builder left the group" in text

    data = _json(get_recent_messages(hours=1, format="json"))
    events = [m["event"] for m in data["messages"] if m.get("event")]
    assert {e["type"] for e in events} == {"renamed", "participant_added", "left"}


def test_outbound_delivery_and_read_receipts(chat_db, alice):
    sent = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    chat_db.add_message(
        "are you up?",
        handle_id=alice,
        is_from_me=1,
        is_sent=1,
        is_delivered=1,
        date=apple_ns(sent),
        date_delivered=apple_ns(sent + timedelta(seconds=5)),
        date_read=apple_ns(sent + timedelta(minutes=12)),
    )
    chat_db.add_message(
        "hello?",
        handle_id=alice,
        is_from_me=1,
        is_sent=1,
        is_delivered=1,
        date=apple_ns(sent + timedelta(minutes=20)),
        date_delivered=apple_ns(sent + timedelta(minutes=20, seconds=3)),
    )

    text = _inner(get_recent_messages(start_date="2026-02-28", end_date="2026-03-02"))
    assert "[read 09:12]: are you up?" in text
    assert "[delivered 09:20]: hello?" in text

    data = _json(
        get_recent_messages(
            start_date="2026-02-28", end_date="2026-03-02", format="json"
        )
    )
    by_body = {m["body"]: m for m in data["messages"]}
    assert by_body["are you up?"]["read_at"].startswith("2026-03-01T09:12")
    assert by_body["hello?"]["read_at"] is None
    assert by_body["hello?"]["delivered_at"].startswith("2026-03-01T09:20")
