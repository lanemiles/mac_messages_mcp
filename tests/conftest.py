# Copyright (c) 2023 Carter Lasalle
"""Shared fixtures: real-schema Messages and AddressBook databases.

See ``tests/chatdb.py``. Tests that want the production schema request
``chat_db`` (and optionally ``address_book``); the mocked tests elsewhere are
unaffected.
"""

from __future__ import annotations

import pytest

from mac_messages_mcp import messages

from .chatdb import AddressBookDb, ChatDb

# Captured before any fixture stubs it, so address_book can restore the real one.
_REAL_GET_CACHED_CONTACTS = messages.get_cached_contacts


@pytest.fixture(scope="session")
def _chat_db_template(tmp_path_factory):
    """Empty real-schema chat.db, built once and copied per test."""
    path = tmp_path_factory.mktemp("schema") / "chat_template.db"
    ChatDb(path).close()
    return path


@pytest.fixture
def chat_db(tmp_path, monkeypatch, _chat_db_template):
    """A schema-faithful chat.db that ``messages`` reads via its normal path.

    Contacts default to empty so senders render as their handle id; request
    ``address_book`` as well to resolve names through the real AddressBook SQL.
    """
    db = ChatDb(tmp_path / "chat.db", template=_chat_db_template)
    monkeypatch.setattr(messages, "get_messages_db_path", lambda: str(db.path))
    monkeypatch.setattr(messages, "get_cached_contacts", dict)
    yield db
    db.close()


@pytest.fixture
def address_book(tmp_path, monkeypatch, chat_db):
    """A schema-faithful AddressBook the real contact-loading SQL runs against."""
    db = AddressBookDb(tmp_path / "AddressBook-v22.abcddb")
    monkeypatch.setattr(
        messages, "_addressbook_db_paths", lambda: ([str(db.path)], str(db.path))
    )
    # Undo chat_db's empty-contacts stub and drop the process-level cache so
    # this test's contacts are what the lookup sees.
    monkeypatch.setattr(messages, "get_cached_contacts", _REAL_GET_CACHED_CONTACTS)
    monkeypatch.setattr(messages, "_CONTACTS_CACHE", None)
    monkeypatch.setattr(messages, "_LAST_CACHE_UPDATE", 0)
    monkeypatch.setattr(messages, "_PHONE_TO_DETAILS_MAP", {})
    yield db
    db.close()
