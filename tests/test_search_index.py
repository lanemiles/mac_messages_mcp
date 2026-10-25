# Copyright (c) 2023 Carter Lasalle
"""Full-history search through a local FTS5 index.

``fuzzy_search_messages`` used to pull at most the newest few thousand
candidate rows and rank them in Python, so a match older than that window was
never found, and on modern macOS where bodies live only in ``attributedBody``
every row is a candidate. The index is built once from chat.db, kept current
incrementally by ROWID, and consulted first; the fuzzy pass then re-ranks.
"""

from __future__ import annotations

import json
import stat

import pytest

from mac_messages_mcp import db, search_index
from mac_messages_mcp.messages import fuzzy_search_messages
from mac_messages_mcp.untrusted import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

from .chatdb import make_attributed_body

ALICE = "+15555550100"


def _inner(fenced: str) -> str:
    assert fenced.startswith(UNTRUSTED_OPEN) and fenced.endswith(UNTRUSTED_CLOSE)
    return (
        fenced[len(UNTRUSTED_OPEN) : -len(UNTRUSTED_CLOSE)].strip().replace("\\n", "\n")
    )


@pytest.fixture
def index_path(tmp_path, monkeypatch):
    path = tmp_path / "index" / "search_index.sqlite"
    monkeypatch.setenv("MAC_MESSAGES_SEARCH_INDEX", str(path))
    search_index.reset()
    yield path
    search_index.reset()


@pytest.fixture
def deep_history(chat_db):
    """600 blob-only messages; the only 'zebra' is the oldest row."""
    alice = chat_db.add_handle(ALICE)
    with chat_db.batch():
        first = chat_db.add_message(
            None,
            handle_id=alice,
            attributedBody=make_attributed_body("unique zebra sighting at the zoo"),
        )
        for i in range(599):
            chat_db.add_message(
                None,
                handle_id=alice,
                attributedBody=make_attributed_body(
                    f"filler message number {i} about lunch"
                ),
            )
    return first


def test_match_older_than_the_candidate_window_is_found(
    chat_db, deep_history, index_path
):
    out = _inner(fuzzy_search_messages("zebra", hours=0, limit=10))
    assert f"#{deep_history} " in out, out
    assert "unique zebra sighting" in out
    assert "Search stopped after" not in out


def test_without_the_index_the_same_search_misses(chat_db, deep_history, monkeypatch):
    # Documents the behaviour the index fixes: the LIKE prefilter cannot see
    # into attributedBody, so the newest 500 rows are the only candidates.
    monkeypatch.setenv("MAC_MESSAGES_SEARCH_INDEX", "none")
    search_index.reset()
    out = _inner(fuzzy_search_messages("zebra", hours=0, limit=10))
    assert "unique zebra sighting" not in out


def test_index_is_incremental(chat_db, deep_history, index_path, monkeypatch):
    fuzzy_search_messages("zebra", hours=0, limit=10)
    status = search_index.status()
    assert status["indexed"] == 600
    assert status["last_rowid"] == 600

    alice = 1
    new = chat_db.add_message("a brand new giraffe", handle_id=alice)
    scanned: list[tuple[str, tuple]] = []
    real = db.query_messages_db

    def spy(query, params=()):
        if "FROM message" in query and "ROWID > ?" in query:
            scanned.append((query, params))
        return real(query, params)

    monkeypatch.setattr(db, "query_messages_db", spy)
    out = _inner(fuzzy_search_messages("giraffe", hours=0, limit=10))
    assert f"#{new} " in out
    assert (
        scanned and scanned[0][1][0] == 600
    ), "refresh starts after the last indexed ROWID"
    assert search_index.status()["indexed"] == 601


def test_index_file_is_private_and_reports_status(chat_db, deep_history, index_path):
    fuzzy_search_messages("zebra", hours=0, limit=10)
    assert index_path.exists()
    assert stat.S_IMODE(index_path.stat().st_mode) == 0o600
    from mac_messages_mcp.messages import check_messages_db_access

    report = check_messages_db_access()
    assert "Search index: 600 messages indexed" in report


def test_index_rebuilds_when_the_source_database_changes(
    chat_db, deep_history, index_path, tmp_path, monkeypatch
):
    fuzzy_search_messages("zebra", hours=0, limit=10)
    assert search_index.status()["indexed"] == 600

    from .chatdb import ChatDb

    (tmp_path / "other").mkdir()
    other = ChatDb(tmp_path / "other" / "chat.db")
    try:
        h = other.add_handle(ALICE)
        other.add_message("only message here, about penguins", handle_id=h)
        monkeypatch.setattr(db, "get_messages_db_path", lambda: str(other.path))
        out = _inner(fuzzy_search_messages("penguins", hours=0, limit=10))
        assert "penguins" in out
        assert (
            search_index.status()["indexed"] == 1
        ), "stale rows from the old database were dropped"
    finally:
        other.close()


def test_scoped_and_windowed_searches_still_apply_their_filters(
    chat_db, index_path, address_book
):
    address_book.add_contact("Alice", "Example", phones=(ALICE,))
    alice = chat_db.add_handle(ALICE)
    bob = chat_db.add_handle("+15555550199")
    chat_db.add_message("zebra from alice", handle_id=alice)
    chat_db.add_message("zebra from bob", handle_id=bob)
    out = _inner(fuzzy_search_messages("zebra", hours=0, contact=ALICE, limit=10))
    assert "zebra from alice" in out and "zebra from bob" not in out

    data = json.loads(
        _inner(fuzzy_search_messages("zebra", hours=0, limit=10, format="json"))
    )
    assert {m["body"] for m in data["messages"]} == {
        "zebra from alice",
        "zebra from bob",
    }


def test_fuzzy_ranking_still_applies_on_top_of_index_hits(chat_db, index_path):
    alice = chat_db.add_handle(ALICE)
    chat_db.add_message("the zebra crossing is closed", handle_id=alice)
    chat_db.add_message("zebra", handle_id=alice)
    data = json.loads(
        _inner(fuzzy_search_messages("zebra", hours=0, limit=10, format="json"))
    )
    scores = [m["score"] for m in data["messages"]]
    assert scores == sorted(scores, reverse=True) and scores[0] == 1.0
