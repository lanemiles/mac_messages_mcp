# Copyright (c) 2023 Carter Lasalle
"""Query shapes on the hot paths, checked with EXPLAIN QUERY PLAN on the real schema.

The fixture carries every index a real chat.db has, so SQLite's plan for a
query here is the plan it picks on a user's Mac. Two shapes used to defeat the
indexes: date filters compared ``CAST(m.date AS TEXT)`` (lexicographic, and a
full index SCAN), and cursor reads ordered by ``date`` while filtering on
``ROWID`` (walks all history on every poll). ``list_conversations`` joined every
message to compute counts.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from mac_messages_mcp import db
from mac_messages_mcp.messages import get_recent_messages, list_conversations
from mac_messages_mcp.untrusted import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

from .chatdb import apple_ns

ALICE = "+15555550100"


def _inner(fenced: str) -> str:
    assert fenced.startswith(UNTRUSTED_OPEN) and fenced.endswith(UNTRUSTED_CLOSE)
    return (
        fenced[len(UNTRUSTED_OPEN) : -len(UNTRUSTED_CLOSE)].strip().replace("\\n", "\n")
    )


class _Capture:
    """Record the SQL + params the read path runs, and EXPLAIN them on the fixture."""

    def __init__(self, chat_db, monkeypatch):
        self.calls: list[tuple[str, tuple]] = []
        self.conn = chat_db.conn
        real = db.query_messages_db

        def spy(query, params=()):
            self.calls.append((" ".join(query.split()), tuple(params)))
            return real(query, params)

        monkeypatch.setattr(db, "query_messages_db", spy)

    def main_query(self, needle: str) -> tuple[str, tuple]:
        matches = [c for c in self.calls if needle in c[0]]
        assert matches, [c[0][:80] for c in self.calls]
        return matches[0]

    def plan(self, sql: str, params: tuple) -> list[tuple[int, int, str]]:
        rows = self.conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        return [(r[0], r[1], r[3]) for r in rows]


@pytest.fixture
def capture(chat_db, monkeypatch):
    return _Capture(chat_db, monkeypatch)


# ---------------------------------------------------------------------------
# 1. Date filters: integer comparisons that use message_idx_date
# ---------------------------------------------------------------------------


def test_seconds_format_row_does_not_leak_into_a_recent_window(chat_db):
    alice = chat_db.add_handle(ALICE)
    chat_db.add_message("today", handle_id=alice)
    # A pre-2017 row stores seconds since 2001. 820000000 s is ~2026 in that
    # unit, but as a 9-digit string it sorts above a 19-digit nanosecond value.
    chat_db.add_message("ancient", handle_id=alice, date=820_000_000)
    out = _inner(get_recent_messages(hours=24))
    assert "today" in out
    assert "ancient" not in out, "lexicographic date compare let an old row through"


def test_hours_window_binds_an_integer_and_seeks_the_date_index(chat_db, capture):
    chat_db.add_message("x", handle_id=chat_db.add_handle(ALICE))
    get_recent_messages(hours=24)
    sql, params = capture.main_query("FROM message m")
    assert "CAST(" not in sql
    assert any(isinstance(p, int) and p > 10**15 for p in params), params
    details = [d for _, _, d in capture.plan(sql, params)]
    assert any("SEARCH m USING" in d and "date>?" in d for d in details), details
    assert not any(d.startswith("SCAN m") for d in details), details


def test_date_range_binds_integers_and_seeks(chat_db, capture):
    alice = chat_db.add_handle(ALICE)
    when = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
    chat_db.add_message("on the day", handle_id=alice, date=apple_ns(when))
    out = _inner(get_recent_messages(start_date="2026-01-15", end_date="2026-01-15"))
    assert "on the day" in out
    sql, params = capture.main_query("FROM message m")
    assert "CAST(" not in sql
    assert all(isinstance(p, int) for p in params[:2]), params
    details = [d for _, _, d in capture.plan(sql, params)]
    assert any(
        "SEARCH m USING" in d and "date>?" in d and "date<?" in d for d in details
    ), details


def test_search_attachments_date_range_binds_integers(chat_db, capture):
    from mac_messages_mcp.messages import search_attachments

    search_attachments(start_date="2026-01-01", end_date="2026-01-02")
    sql, params = capture.main_query("message_attachment_join")
    assert "CAST(" not in sql
    assert all(isinstance(p, int) for p in params[:2]), params


# ---------------------------------------------------------------------------
# 1b. Cursor reads: a primary-key seek, not a walk of the date index
# ---------------------------------------------------------------------------


def test_cursor_read_seeks_by_rowid(chat_db, capture):
    alice = chat_db.add_handle(ALICE)
    for i in range(5):
        chat_db.add_message(f"m{i}", handle_id=alice)
    out = _inner(get_recent_messages(since_rowid=3))
    assert "m3" in out and "m4" in out and "m2" not in out
    sql, params = capture.main_query("m.ROWID > ?")
    assert "ORDER BY m.ROWID ASC" in sql, sql
    details = [d for _, _, d in capture.plan(sql, params)]
    assert any(
        "SEARCH m USING INTEGER PRIMARY KEY (rowid>?)" in d for d in details
    ), details
    assert not any("message_idx_date" in d for d in details), details


def test_cursor_read_returns_rows_in_rowid_order_even_if_dates_disagree(chat_db):
    alice = chat_db.add_handle(ALICE)
    now = apple_ns()
    chat_db.add_message(
        "first by rowid, newest by date", handle_id=alice, date=now + 10**9
    )
    chat_db.add_message("second by rowid, older by date", handle_id=alice, date=now)
    out = _inner(get_recent_messages(since_rowid=0))
    lines = [l for l in out.splitlines() if l.startswith("[")]
    assert "#1 " in lines[0] and "#2 " in lines[1]


# ---------------------------------------------------------------------------
# 2. list_conversations: no per-message lookups, archived hidden, filtered tagged,
#    unseen-since-last-opened
# ---------------------------------------------------------------------------


def _ancestors(plan, node_id):
    """Plan nodes that scope ``node_id``: its ancestors and the earlier
    siblings in the same loop nest (an outer loop's predicate bounds the inner
    lookup's cardinality)."""
    by_id = {n[0]: n for n in plan}
    chain = []
    parent = by_id[node_id][1]
    for nid, par, detail in plan:
        if par == parent and nid < node_id:
            chain.append(detail)
    while node_id in by_id and by_id[node_id][1] != 0:
        parent = by_id[node_id][1]
        chain.append(by_id[parent][2])
        node_id = parent
    return chain


def test_list_conversations_does_not_visit_every_message(chat_db, capture):
    alice = chat_db.add_handle(ALICE)
    chat = chat_db.add_chat(ALICE, handles=(alice,))
    for i in range(5):
        chat_db.add_message(f"m{i}", handle_id=alice, chat_id=chat, is_read=1)
    list_conversations()
    sql, params = capture.main_query("FROM chat c")
    plan = capture.plan(sql, params)
    # Every access to the message table must sit under a filtering predicate
    # (unread flags or a per-chat message_date bound). A bare primary-key
    # lookup driven by the chat/join scan means one lookup per message.
    for node_id, _parent, detail in plan:
        if " m USING" in detail or detail.startswith("SCAN m"):
            context = [detail, *_ancestors(plan, node_id)]
            assert any(
                "is_read" in c or "message_date>" in c or "message_date<" in c
                for c in context
            ), f"unfiltered message access: {detail} under {context}"


def test_list_conversations_hides_archived_and_tags_filtered(chat_db):
    alice = chat_db.add_handle(ALICE)
    bob = chat_db.add_handle("+15555550199")
    spam = chat_db.add_handle("+15555550111")
    live = chat_db.add_chat(ALICE, handles=(alice,))
    archived = chat_db.add_chat("+15555550199", handles=(bob,))
    unknown = chat_db.add_chat("+15555550111", handles=(spam,))
    chat_db.conn.execute("UPDATE chat SET is_archived = 1 WHERE ROWID = ?", (archived,))
    chat_db.conn.execute("UPDATE chat SET is_filtered = 1 WHERE ROWID = ?", (unknown,))
    chat_db.conn.commit()
    chat_db.add_message("hi", handle_id=alice, chat_id=live)
    chat_db.add_message("old", handle_id=bob, chat_id=archived)
    chat_db.add_message("you won", handle_id=spam, chat_id=unknown)

    out = _inner(list_conversations())
    assert ALICE in out
    assert "+15555550199" not in out, "archived threads are hidden by default"
    assert "[unknown sender]" in out and "+15555550111" in out

    with_archived = _inner(list_conversations(include_archived=True))
    assert "+15555550199" in with_archived and "[archived]" in with_archived

    data = json.loads(_inner(list_conversations(include_archived=True, format="json")))
    by_id = {c["chat_id"]: c for c in data["conversations"]}
    assert by_id["+15555550199"]["archived"] is True
    assert by_id["+15555550111"]["filtered"] is True
    assert by_id[ALICE]["archived"] is False and by_id[ALICE]["filtered"] is False


def test_list_conversations_counts_unseen_since_last_opened(chat_db):
    alice = chat_db.add_handle(ALICE)
    chat = chat_db.add_chat(ALICE, handles=(alice,))
    t0 = apple_ns(datetime.now(timezone.utc) - timedelta(hours=2))
    chat_db.add_message("seen", handle_id=alice, chat_id=chat, date=t0, is_read=1)
    chat_db.conn.execute(
        "UPDATE chat SET last_read_message_timestamp = ? WHERE ROWID = ?",
        (t0 + 1, chat),
    )
    chat_db.conn.commit()
    # Two newer inbound messages; Apple's per-message is_read flag claims one
    # was read, but the thread has not been opened since t0.
    chat_db.add_message("new 1", handle_id=alice, chat_id=chat, is_read=1)
    chat_db.add_message("new 2", handle_id=alice, chat_id=chat, is_read=0)
    chat_db.add_message("mine", handle_id=alice, chat_id=chat, is_from_me=1)

    out = _inner(list_conversations())
    assert "1 unread" in out
    assert "2 since last opened" in out

    data = json.loads(_inner(list_conversations(format="json")))
    conv = data["conversations"][0]
    assert conv["unread_count"] == 1
    assert conv["unseen_since_last_opened"] == 2
    assert conv["last_opened_at"] is not None
