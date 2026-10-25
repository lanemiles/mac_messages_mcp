# Copyright (c) 2023 Carter Lasalle
"""Chat lookups are cached, and the iMessage availability check is constant time.

``get_chat_mapping`` re-read the whole chat table on every page, and
``_find_chat_by_identifier`` re-ran its lookup on every scoped call.
``_check_imessage_availability`` counted every message a handle ever sent to
answer a yes/no question.
"""

from __future__ import annotations

import pytest

from mac_messages_mcp import chats, db
from mac_messages_mcp.messages import (
    get_recent_messages,
)
from mac_messages_mcp.send import _check_imessage_availability

ALICE = "+15555550100"
BOB = "+15555550199"


class _Spy:
    def __init__(self, monkeypatch):
        self.queries: list[tuple[str, tuple]] = []
        real = db.query_messages_db

        def spy(query, params=()):
            self.queries.append((" ".join(query.split()), tuple(params)))
            return real(query, params)

        monkeypatch.setattr(db, "query_messages_db", spy)

    def count(self, needle: str) -> int:
        return sum(1 for q, _ in self.queries if needle in q)

    def reset(self):
        self.queries.clear()


@pytest.fixture
def spy(monkeypatch):
    return _Spy(monkeypatch)


def test_chat_mapping_is_cached_across_reads(chat_db, spy):
    alice = chat_db.add_handle(ALICE)
    group = chat_db.add_chat("chat1", style=43, display_name="Family", handles=(alice,))
    chat_db.add_message("hi", handle_id=alice, chat_id=group, cache_roomnames="chat1")
    assert "[Family]" in get_recent_messages(hours=1)
    assert spy.count("room_name, display_name FROM chat") == 1
    spy.reset()
    assert "[Family]" in get_recent_messages(hours=1)
    assert (
        spy.count("room_name, display_name FROM chat") == 0
    ), "second page re-read the chat table"


def test_chat_mapping_cache_notices_a_new_chat(chat_db, spy):
    alice = chat_db.add_handle(ALICE)
    get_recent_messages(hours=1)
    group = chat_db.add_chat(
        "chat9", style=43, display_name="New Group", handles=(alice,)
    )
    chat_db.add_message(
        "hello", handle_id=alice, chat_id=group, cache_roomnames="chat9"
    )
    assert "[New Group]" in get_recent_messages(hours=1)


def test_find_chat_by_identifier_is_cached(chat_db, spy):
    alice = chat_db.add_handle(ALICE)
    chat_db.add_chat("chat1", style=43, display_name="Family", handles=(alice,))
    first = chats._find_chat_by_identifier("chat1")
    assert first and first["display_name"] == "Family"
    spy.reset()
    second = chats._find_chat_by_identifier("iMessage;+;chat1")
    assert second and second["ROWID"] == first["ROWID"]
    assert spy.count("chat_identifier IN") == 0, "lookup should come from the cache"
    assert chats._find_chat_by_identifier("chat-nope") is None


def test_availability_check_is_an_exists_not_a_count(chat_db, spy):
    alice = chat_db.add_handle(ALICE, service="iMessage")
    bob = chat_db.add_handle(BOB, service="SMS")
    carol = chat_db.add_handle("+15555550177", service="iMessage")
    with chat_db.batch():
        for i in range(200):
            chat_db.add_message(f"m{i}", handle_id=alice, error=0)
        chat_db.add_message("sms", handle_id=bob, service="SMS")
        chat_db.add_message("failed", handle_id=carol, error=22)
    assert _check_imessage_availability(ALICE) is True
    assert _check_imessage_availability(BOB) is False
    assert _check_imessage_availability("+15555550177") is False, "only errored traffic"
    availability_sql = [
        q for q, _ in spy.queries if "iMessage" in q or "h.service" in q
    ]
    assert availability_sql, [q[:80] for q, _ in spy.queries]
    assert all("COUNT(" not in q for q in availability_sql), availability_sql
    assert any(
        "LIMIT 1" in q or "EXISTS" in q for q in availability_sql
    ), availability_sql
