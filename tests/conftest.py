# Copyright (c) 2023 Carter Lasalle
"""Shared fixtures: real-schema Messages and AddressBook databases.

See ``tests/chatdb.py``. Tests that want the production schema request
``chat_db`` (and optionally ``address_book``); the mocked tests elsewhere are
unaffected.
"""

from __future__ import annotations

from datetime import timezone

import pytest

from mac_messages_mcp import contacts, db, messages, session

from .chatdb import AddressBookDb, ChatDb

# Captured before any fixture stubs it, so address_book can restore the real one.
_REAL_GET_CACHED_CONTACTS = contacts.get_cached_contacts


@pytest.fixture(autouse=True)
def _reset_process_caches(monkeypatch, tmp_path_factory):
    """Caches are keyed by database path; tests reuse paths and handle ROWIDs.

    Attachment tests send files from pytest's temp directory, so that root is
    allowed by default here; the policy tests override it explicitly.
    """
    session.Session().reset()
    # Displayed times and local-day filters go through db.local_timezone().
    # Pin it so results do not depend on the timezone of the machine running
    # the suite (a Mac in New York and a CI runner in UTC must agree). Tests
    # about timezone behaviour override this with their own zone.
    monkeypatch.setattr(db, "local_timezone", lambda: timezone.utc)
    monkeypatch.setenv(
        "MAC_MESSAGES_ATTACHMENT_ROOTS", str(tmp_path_factory.getbasetemp())
    )
    monkeypatch.setenv("MAC_MESSAGES_SCHEDULE_STORE", "none")
    monkeypatch.setenv("MAC_MESSAGES_SEARCH_INDEX", "none")
    for var in (
        "MAC_MESSAGES_READ_ONLY",
        "MAC_MESSAGES_REQUIRE_CONFIRM",
        "MAC_MESSAGES_MAX_SENDS_PER_HOUR",
        "MAC_MESSAGES_ALLOWED_RECIPIENTS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    session.Session().reset()


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
    database = ChatDb(tmp_path / "chat.db", template=_chat_db_template)
    monkeypatch.setattr(db, "get_messages_db_path", lambda: str(database.path))
    monkeypatch.setattr(contacts, "get_cached_contacts", dict)
    yield database
    database.close()


@pytest.fixture
def address_book(tmp_path, monkeypatch, chat_db):
    """A schema-faithful AddressBook the real contact-loading SQL runs against."""
    database = AddressBookDb(tmp_path / "AddressBook-v22.abcddb")
    monkeypatch.setattr(
        contacts,
        "_addressbook_db_paths",
        lambda: ([str(database.path)], str(database.path)),
    )
    # Undo chat_db's empty-contacts stub and drop the process-level cache so
    # this test's contacts are what the lookup sees.
    monkeypatch.setattr(contacts, "get_cached_contacts", _REAL_GET_CACHED_CONTACTS)
    monkeypatch.setattr(contacts, "_CONTACTS_CACHE", None)
    monkeypatch.setattr(contacts, "_LAST_CACHE_UPDATE", 0)
    monkeypatch.setattr(contacts, "_PHONE_TO_DETAILS_MAP", {})
    yield database
    database.close()
