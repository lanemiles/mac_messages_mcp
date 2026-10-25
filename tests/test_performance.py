# Copyright (c) 2023 Carter Lasalle
"""Database-access budgets for the read path.

Measured against the real-schema fixture rather than mocks, so the numbers
reflect what a client actually pays: how many SQLite connections a read opens
and how many queries it runs. Before these budgets existed a read opened a
fresh connection for every query and resolved each sender's name with one or
two queries per message, so a 1,000-message page cost ~2,000 connections.
"""

from __future__ import annotations

import sqlite3

import pytest

from mac_messages_mcp import contacts, db
from mac_messages_mcp.messages import (
    find_handles_by_phone,
    fuzzy_search_messages,
    get_recent_messages,
)

SENDERS = [f"+1555555010{i}" for i in range(5)]


@pytest.fixture
def populated(chat_db, address_book):
    handles = [chat_db.add_handle(number) for number in SENDERS]
    for index, number in enumerate(SENDERS):
        address_book.add_contact(f"Person{index}", "Example", phones=(number,))
    for i in range(50):
        chat_db.add_message(f"message {i} hello", handle_id=handles[i % 5])
    return handles


class _Counter:
    """Count sqlite connects and query_messages_db calls, by SQL text."""

    def __init__(self, monkeypatch):
        self.connects = 0
        self.queries: list[str] = []
        real_connect = sqlite3.connect
        real_query = db.query_messages_db

        def counting_connect(*args, **kwargs):
            # Only the Messages database; AddressBook loads are cached 5 min.
            if "chat.db" in str(args[0]):
                self.connects += 1
            return real_connect(*args, **kwargs)

        def counting_query(query, params=()):
            self.queries.append(" ".join(query.split()))
            return real_query(query, params)

        monkeypatch.setattr(db.sqlite3, "connect", counting_connect)
        monkeypatch.setattr(db, "query_messages_db", counting_query)

    def reset(self):
        self.connects = 0
        self.queries.clear()


@pytest.fixture
def counter(monkeypatch):
    return _Counter(monkeypatch)


def test_a_read_reuses_one_connection(populated, counter):
    out = get_recent_messages(hours=1, limit=50)
    assert "Person3 Example" in out
    # One connection for the whole read, not one per query.
    assert counter.connects <= 2, f"{counter.connects} connections opened"


def test_sender_names_are_resolved_in_a_batch_not_per_message(populated, counter):
    get_recent_messages(hours=1, limit=50)
    handle_lookups = [q for q in counter.queries if "FROM handle" in q]
    # Previously one `SELECT id FROM handle WHERE ROWID = ?` per rendered
    # message (50 here). A batch needs one.
    assert len(handle_lookups) <= 2, f"{len(handle_lookups)} handle lookups"
    assert len(counter.queries) <= 8, f"{len(counter.queries)} queries for one page"


def test_second_read_hits_the_name_cache(populated, counter):
    get_recent_messages(hours=1, limit=50)
    counter.reset()
    out = get_recent_messages(hours=1, limit=50)
    assert "Person0 Example" in out
    assert not [q for q in counter.queries if "FROM handle" in q]
    assert counter.connects == 0


def test_fuzzy_search_shares_the_same_budget(populated, counter):
    out = fuzzy_search_messages("hello", hours=1, limit=50)
    assert "Person1 Example" in out
    assert counter.connects <= 2
    assert len([q for q in counter.queries if "FROM handle" in q]) <= 2


def test_canonical_handle_miss_does_not_rescan_the_table_each_time(populated, counter):
    # A number with no handle falls through to a scan of every handle row,
    # canonicalising each one. The second miss must reuse that work.
    assert find_handles_by_phone("+15550009999") is None
    assert find_handles_by_phone("+15550008888") is None
    scans = [q for q in counter.queries if q == "SELECT ROWID, id FROM handle"]
    assert len(scans) <= 1, f"full handle scan ran {len(scans)} times"


def test_name_cache_is_per_database(chat_db, address_book, counter, monkeypatch):
    # The cache must never serve a name recorded for a different chat.db.
    address_book.add_contact("Alice", "Example", phones=(SENDERS[0],))
    alice = chat_db.add_handle(SENDERS[0])
    assert contacts.get_contact_name(alice) == "Alice Example"
    monkeypatch.setattr(db, "get_messages_db_path", lambda: "/nonexistent/other.db")
    assert contacts.get_contact_name(alice) == "Unknown"
