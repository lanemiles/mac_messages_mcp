# Copyright (c) 2023 Carter Lasalle
"""Who a message is from, who it is to, and which thread it belongs to.

An unfiltered read used to render every outbound message as ``You:`` with no
recipient, tag only *named* group chats (most groups have no name), and
``tool_get_chats`` listed only named chats. All of this is resolvable from
chat.db: the counterpart handle on a 1:1 outbound row, and chat_handle_join
for a group's participants.
"""

from __future__ import annotations

import json

import pytest

from mac_messages_mcp.messages import get_recent_messages, list_named_chats
from mac_messages_mcp.untrusted import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

ALICE = "+15555550100"
BOB = "+15555550199"
CAROL = "+15555550177"


def _inner(fenced: str) -> str:
    assert fenced.startswith(UNTRUSTED_OPEN) and fenced.endswith(UNTRUSTED_CLOSE)
    return (
        fenced[len(UNTRUSTED_OPEN) : -len(UNTRUSTED_CLOSE)].strip().replace("\\n", "\n")
    )


def _json(fenced: str):
    return json.loads(_inner(fenced))


@pytest.fixture
def world(chat_db, address_book):
    address_book.add_contact("Alice", "Example", phones=(ALICE,))
    address_book.add_contact("Bob", "Builder", phones=(BOB,))
    alice = chat_db.add_handle(ALICE)
    bob = chat_db.add_handle(BOB)
    carol = chat_db.add_handle(CAROL)  # not in the address book
    direct = chat_db.add_chat(ALICE, style=45, handles=(alice,))
    unnamed = chat_db.add_chat("chat555000111", style=43, handles=(alice, bob, carol))
    named = chat_db.add_chat(
        "chat777000222", style=43, display_name="Family", handles=(alice, bob)
    )
    return {
        "alice": alice,
        "bob": bob,
        "carol": carol,
        "direct": direct,
        "unnamed": unnamed,
        "named": named,
    }


# ---------------------------------------------------------------------------
# Outbound messages name their recipient
# ---------------------------------------------------------------------------


def test_outbound_one_to_one_message_names_the_recipient(chat_db, world):
    # On a 1:1 outbound row, handle_id is the counterpart.
    chat_db.add_message(
        "on my way", handle_id=world["alice"], is_from_me=1, chat_id=world["direct"]
    )

    text = _inner(get_recent_messages(hours=1))
    assert "You → Alice Example [iMessage]: on my way" in text

    data = _json(get_recent_messages(hours=1, format="json"))
    msg = data["messages"][0]
    assert msg["is_from_me"] is True
    assert msg["to"] == {
        "name": "Alice Example",
        "handle_id": world["alice"],
        "address": ALICE,
    }
    assert msg["chat"]["id"] == ALICE and msg["chat"]["kind"] == "direct"


def test_outbound_group_message_has_no_arrow_but_names_the_group(chat_db, world):
    chat_db.add_message("hi all", handle_id=0, is_from_me=1, chat_id=world["named"])
    text = _inner(get_recent_messages(hours=1))
    assert "[Family] You [iMessage]: hi all" in text
    assert "→" not in text


# ---------------------------------------------------------------------------
# Unnamed group chats are labelled by their participants
# ---------------------------------------------------------------------------


def test_unnamed_group_is_labelled_with_participants(chat_db, world):
    chat_db.add_message("lunch?", handle_id=world["bob"], chat_id=world["unnamed"])

    text = _inner(get_recent_messages(hours=1))
    assert (
        f"[group: Alice Example, Bob Builder, {CAROL}] Bob Builder [iMessage] [unread]: lunch?"
        in text
    )

    data = _json(get_recent_messages(hours=1, format="json"))
    chat = data["messages"][0]["chat"]
    assert chat["id"] == "chat555000111"
    assert chat["name"] is None and chat["kind"] == "group"
    assert [p["name"] for p in chat["participants"]] == [
        "Alice Example",
        "Bob Builder",
        CAROL,
    ]
    assert chat["participants"][2]["address"] == CAROL


def test_large_unnamed_group_label_is_capped(chat_db, address_book):
    handles = [chat_db.add_handle(f"+1555555020{i}") for i in range(6)]
    for i, number in enumerate(f"+1555555020{i}" for i in range(6)):
        address_book.add_contact(f"P{i}", "X", phones=(number,))
    big = chat_db.add_chat("chat999", style=43, handles=tuple(handles))
    chat_db.add_message("hey", handle_id=handles[0], chat_id=big)
    text = _inner(get_recent_messages(hours=1))
    assert "[group: P0 X, P1 X, P2 X +3]" in text


# ---------------------------------------------------------------------------
# tool_get_chats and tool_get_chat_participants cover unnamed groups
# ---------------------------------------------------------------------------


def test_get_chats_lists_unnamed_groups_by_participants(chat_db, world):
    from mac_messages_mcp.server import tool_get_chats

    out = _inner(tool_get_chats())
    assert "Family (ID: chat777000222)" in out
    assert f"(unnamed: Alice Example, Bob Builder, {CAROL}) (ID: chat555000111)" in out
    # 1:1 threads are not group chats.
    assert f"(ID: {ALICE})" not in out


def test_list_named_chats_rows_carry_participants_for_unnamed_groups(chat_db, world):
    rows = list_named_chats()
    by_id = {r["chat_identifier"]: r for r in rows}
    assert by_id["chat777000222"]["display_name"] == "Family"
    assert by_id["chat555000111"]["display_name"] in (None, "")
    assert [p["name"] for p in by_id["chat555000111"]["participants"]][:2] == [
        "Alice Example",
        "Bob Builder",
    ]


def test_get_chat_participants_tool(chat_db, world):
    from mac_messages_mcp.server import tool_get_chat_participants

    out = _inner(tool_get_chat_participants(chat_id="chat555000111"))
    assert "3 participant(s)" in out
    assert f"Alice Example ({ALICE})" in out
    assert f"{CAROL}" in out

    data = _json(
        tool_get_chat_participants(chat_id="iMessage;+;chat555000111", format="json")
    )
    assert data["chat"]["id"] == "chat555000111"
    assert {p["address"] for p in data["participants"]} == {ALICE, BOB, CAROL}

    missing = _inner(tool_get_chat_participants(chat_id="chat000"))
    assert missing.startswith("No chat found")
