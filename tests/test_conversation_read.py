# Copyright (c) 2023 Carter Lasalle
"""Thread-first reading: one conversation, paged by ROWID cursors.

Agents think in threads ("catch me up on the family group"). ``get_recent_messages``
is message-first and newest-first; ``get_conversation`` returns one chat's
messages in chronological order with cursors to page older or newer, plus the
chat's name, kind and participants. Group @-mentions of the user
(``message.has_unseen_mention``) are surfaced on messages and conversations.
"""

from __future__ import annotations

import json

import pytest

from mac_messages_mcp.messages import (
    get_conversation,
    get_recent_messages,
    list_conversations,
)
from mac_messages_mcp.untrusted import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

ALICE = "+15555550100"
BOB = "+15555550199"


def _inner(fenced: str) -> str:
    assert fenced.startswith(UNTRUSTED_OPEN) and fenced.endswith(UNTRUSTED_CLOSE)
    return (
        fenced[len(UNTRUSTED_OPEN) : -len(UNTRUSTED_CLOSE)].strip().replace("\\n", "\n")
    )


def _message_lines(fenced: str) -> list[str]:
    return [l for l in _inner(fenced).splitlines() if l.startswith("[20")]


@pytest.fixture
def family(chat_db, address_book):
    address_book.add_contact("Alice", "Example", phones=(ALICE,))
    address_book.add_contact("Bob", "Builder", phones=(BOB,))
    alice = chat_db.add_handle(ALICE)
    bob = chat_db.add_handle(BOB)
    group = chat_db.add_chat(
        "chat555", style=43, display_name="Family", handles=(alice, bob)
    )
    other = chat_db.add_chat(ALICE, style=45, handles=(alice,))
    ids = []
    with chat_db.batch():
        for i in range(12):
            sender = alice if i % 2 else bob
            ids.append(
                chat_db.add_message(f"group {i}", handle_id=sender, chat_id=group)
            )
        chat_db.add_message("direct, not in the group", handle_id=alice, chat_id=other)
    return {"group": group, "ids": ids, "alice": alice, "bob": bob}


def test_default_page_is_the_newest_messages_in_chronological_order(chat_db, family):
    out = get_conversation("chat555", limit=5)
    lines = _message_lines(out)
    assert [l.split(": ", 1)[1] for l in lines] == [
        "group 7",
        "group 8",
        "group 9",
        "group 10",
        "group 11",
    ]
    inner = _inner(out)
    assert inner.startswith("Family (chat ID: chat555) [group]")
    assert "Alice Example, Bob Builder" in inner
    assert "direct, not in the group" not in inner
    assert (
        f"before_rowid={family['ids'][7]}" in inner
    ), "footer names the cursor for older messages"
    assert f"after_rowid={family['ids'][11]}" in inner


def test_paging_older_and_newer(chat_db, family):
    ids = family["ids"]
    older = get_conversation("chat555", limit=5, before_rowid=ids[7])
    assert [l.split(": ", 1)[1] for l in _message_lines(older)] == [
        "group 2",
        "group 3",
        "group 4",
        "group 5",
        "group 6",
    ]

    newer = get_conversation("chat555", limit=5, after_rowid=ids[6])
    assert [l.split(": ", 1)[1] for l in _message_lines(newer)] == [
        "group 7",
        "group 8",
        "group 9",
        "group 10",
        "group 11",
    ]

    start = get_conversation("chat555", limit=5, before_rowid=ids[2])
    assert [l.split(": ", 1)[1] for l in _message_lines(start)] == [
        "group 0",
        "group 1",
    ]
    assert "before_rowid=" not in _inner(
        start
    ), "no older cursor at the beginning of the thread"


def test_accepts_the_long_chat_id_spelling_and_rejects_unknown(chat_db, family):
    assert "group 11" in _inner(get_conversation("iMessage;+;chat555", limit=1))
    missing = _inner(get_conversation("chat000"))
    assert missing.startswith("No chat found")
    assert "not both" in _inner(
        get_conversation("chat555", before_rowid=5, after_rowid=1)
    )


def test_json_shape(chat_db, family):
    data = json.loads(_inner(get_conversation("chat555", limit=3, format="json")))
    assert data["chat"]["id"] == "chat555" and data["chat"]["kind"] == "group"
    assert [p["name"] for p in data["chat"]["participants"]] == [
        "Alice Example",
        "Bob Builder",
    ]
    assert [m["body"] for m in data["messages"]] == ["group 9", "group 10", "group 11"]
    assert data["cursors"] == {
        "before_rowid": family["ids"][9],
        "after_rowid": family["ids"][11],
        "has_older": True,
    }


def test_tool_is_registered_and_fenced(chat_db, family):
    from mac_messages_mcp.server import mcp, tool_get_conversation

    assert "tool_get_conversation" in {t.name for t in mcp._tool_manager.list_tools()}
    assert "group 11" in _inner(tool_get_conversation(chat_id="chat555", limit=2))


def test_unseen_mentions_are_surfaced(chat_db, family):
    rowid = chat_db.add_message(
        "@Lane can you bring the chairs?",
        handle_id=family["bob"],
        chat_id=family["group"],
        has_unseen_mention=1,
        is_read=0,
    )
    out = _inner(get_conversation("chat555", limit=2))
    assert "[mentions you]: @Lane can you bring the chairs?" in out

    data = json.loads(_inner(get_recent_messages(hours=1, format="json")))
    mention = [m for m in data["messages"] if m["rowid"] == rowid][0]
    assert mention["mentions_me"] is True

    convos = _inner(list_conversations())
    assert (
        "[mentions you]" in convos.split("Family")[0]
        or "[mentions you] Family" in convos
    )
    convo_json = json.loads(_inner(list_conversations(format="json")))
    fam = [c for c in convo_json["conversations"] if c["chat_id"] == "chat555"][0]
    assert fam["unseen_mentions"] == 1
