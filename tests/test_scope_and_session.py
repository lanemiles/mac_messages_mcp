# Copyright (c) 2023 Carter Lasalle
"""Shape of filter resolution and of process-level state.

``_resolve_message_scope`` returned a five-tuple that three callers unpacked
and then each rebuilt the same SQL from. It now returns a ``Scope`` with named
fields and one ``sql_clauses`` helper (it still unpacks as a tuple). Caches
that lived as module globals across six modules are cleared through one
``Session.reset()``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mac_messages_mcp import chats, contacts, db, read, scope, search_index, session
from mac_messages_mcp.messages import (
    get_recent_messages,
)
from mac_messages_mcp.scope import resolve_scope as _resolve_message_scope

ALICE = "+15555550100"
PACKAGE = Path(read.__file__).parent


def test_scope_has_named_fields_and_still_unpacks(chat_db):
    alice = chat_db.add_handle(ALICE)
    chat_db.add_message("hi", handle_id=alice)
    resolved = _resolve_message_scope(ALICE, None)
    assert isinstance(resolved, scope.Scope)
    assert resolved.handle_ids == [alice]
    assert resolved.chat_row_id is None and resolved.error is None
    handle_ids, chat_row_id, name, is_group, error = resolved  # legacy unpacking
    assert handle_ids == [alice] and chat_row_id is None and error is None


def test_scope_builds_the_filter_sql_once_for_everyone(chat_db):
    alice = chat_db.add_handle(ALICE)
    group = chat_db.add_chat("chat1", style=43, display_name="Family", handles=(alice,))
    by_handle = _resolve_message_scope(ALICE, None)
    clauses, params = by_handle.sql_clauses()
    # One clause: the contact's handle, OR a message in a 1:1 chat with that
    # handle (the user's own synced replies carry handle_id = 0).
    assert len(clauses) == 1
    assert clauses[0].startswith("(m.handle_id IN (?) OR m.ROWID IN (")
    assert "c.style = 45" in clauses[0] and "chj.handle_id IN (?)" in clauses[0]
    assert params == [alice, alice]

    by_chat = _resolve_message_scope(None, "chat1")
    clauses, params = by_chat.sql_clauses()
    assert clauses == [
        "m.ROWID IN (SELECT message_id FROM chat_message_join WHERE chat_id = ?)"
    ]
    assert params == [group]
    assert by_chat.chat_is_group is True and by_chat.chat_display_name == "Family"

    assert _resolve_message_scope(None, None).sql_clauses() == ([], [])
    failed = _resolve_message_scope(ALICE, "chat1")
    assert failed.error and "not both" in failed.error


def test_scope_resolution_lives_in_its_own_module_and_read_is_smaller():
    assert (PACKAGE / "scope.py").exists()
    assert "def _resolve_message_scope" not in (PACKAGE / "read.py").read_text()
    assert len((PACKAGE / "read.py").read_text().splitlines()) < 1100


def test_session_reset_clears_every_cache(chat_db, address_book, tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_MESSAGES_SEARCH_INDEX", str(tmp_path / "i.sqlite"))
    search_index.reset()
    address_book.add_contact("Alice", "Example", phones=(ALICE,))
    alice = chat_db.add_handle(ALICE)
    chat_db.add_chat("chat1", style=43, display_name="Family", handles=(alice,))
    chat_db.add_message("warm the caches", handle_id=alice)
    get_recent_messages(hours=1)
    chats._find_chat_by_identifier("chat1")
    search_index.candidate_rowids("warm", 10)

    assert (
        contacts._HANDLE_NAME_CACHE
        and chats._CHAT_MAPPING_CACHE
        and chats._CHAT_LOOKUP_CACHE
    )
    assert (
        read._MESSAGE_COLUMNS_CACHE
        and getattr(db._LOCAL, "connection", None) is not None
    )
    assert search_index._connection is not None

    session.Session().reset()

    assert not contacts._HANDLE_NAME_CACHE and not contacts._HANDLE_ADDRESS_CACHE
    assert not chats._CHAT_MAPPING_CACHE and not chats._CHAT_LOOKUP_CACHE
    assert not read._MESSAGE_COLUMNS_CACHE
    assert getattr(db._LOCAL, "connection", None) is None
    assert search_index._connection is None
    assert session.Session().status()["handle_names_cached"] == 0


def test_conftest_uses_the_session_reset():
    text = (Path(__file__).parent / "conftest.py").read_text()
    assert "session.Session().reset()" in text or "session.reset_all_caches()" in text
    assert (
        "clear_handle_name_cache()" not in text
    ), "the per-module ritual should be gone"
