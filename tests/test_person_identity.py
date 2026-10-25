# Copyright (c) 2023 Carter Lasalle
"""Apple's own grouping of a person's handles: ``handle.person_centric_id``.

Messages assigns one person_centric_id to every handle (phone numbers and
emails) it believes belong to the same person. The server used to rebuild that
grouping itself from the AddressBook, which fails for anyone not in Contacts
and for handles the card does not list. Reads, attachment searches, and name
resolution now follow Apple's grouping first.
"""

from __future__ import annotations

import json

import pytest

from mac_messages_mcp import contacts
from mac_messages_mcp.messages import get_recent_messages, search_attachments
from mac_messages_mcp.untrusted import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

ALICE_PHONE = "+15555550100"
ALICE_EMAIL = "alice@example.com"
BOB = "+15555550199"


def _inner(fenced: str) -> str:
    assert fenced.startswith(UNTRUSTED_OPEN) and fenced.endswith(UNTRUSTED_CLOSE)
    return (
        fenced[len(UNTRUSTED_OPEN) : -len(UNTRUSTED_CLOSE)].strip().replace("\\n", "\n")
    )


@pytest.fixture
def alice(chat_db):
    """Alice's phone and email share a person_centric_id; Bob has his own."""
    phone = chat_db.add_handle(ALICE_PHONE)
    email = chat_db.add_handle(ALICE_EMAIL)
    bob = chat_db.add_handle(BOB)
    chat_db.conn.execute(
        "UPDATE handle SET person_centric_id = 'PCID-ALICE' WHERE ROWID IN (?, ?)",
        (phone, email),
    )
    chat_db.conn.execute(
        "UPDATE handle SET person_centric_id = 'PCID-BOB' WHERE ROWID = ?", (bob,)
    )
    chat_db.conn.commit()
    chat_db.add_message("from alice's phone", handle_id=phone)
    chat_db.add_message("from alice's email", handle_id=email)
    chat_db.add_message("from bob", handle_id=bob)
    return {"phone": phone, "email": email, "bob": bob}


def test_reading_by_phone_covers_the_persons_other_handles(chat_db, alice):
    # No AddressBook at all: only Apple's grouping can connect the two handles.
    out = _inner(get_recent_messages(hours=1, contact=ALICE_PHONE))
    assert "from alice's phone" in out
    assert "from alice's email" in out
    assert "from bob" not in out


def test_reading_by_email_covers_the_phone_too(chat_db, alice):
    out = _inner(get_recent_messages(hours=1, contact=ALICE_EMAIL))
    assert "from alice's phone" in out and "from alice's email" in out
    assert "from bob" not in out


def test_name_resolves_through_a_sibling_handle(chat_db, alice, address_book):
    # The card lists only the phone; the email handle gets the name anyway.
    address_book.add_contact("Alice", "Example", phones=(ALICE_PHONE,))
    assert contacts.get_contact_name(alice["email"]) == "Alice Example"
    assert contacts.get_contact_name(alice["bob"]) == BOB


def test_handles_without_a_person_id_are_not_merged(chat_db):
    a = chat_db.add_handle(ALICE_PHONE)
    b = chat_db.add_handle(
        ALICE_EMAIL
    )  # same person in reality, but Apple has not linked them
    chat_db.add_message("phone", handle_id=a)
    chat_db.add_message("email", handle_id=b)
    out = _inner(get_recent_messages(hours=1, contact=ALICE_PHONE))
    assert "phone" in out and "email" not in out


def test_attachment_search_by_contact_follows_the_grouping(chat_db, alice, tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("x")
    msg = chat_db.add_message("file", handle_id=alice["email"])
    chat_db.add_attachment(msg, str(f), "text/plain")
    out = _inner(search_attachments(contact=ALICE_PHONE))
    assert "x.txt" in out


def test_json_sender_carries_the_person_id(chat_db, alice):
    data = json.loads(_inner(get_recent_messages(hours=1, format="json")))
    by_body = {m["body"]: m for m in data["messages"]}
    assert by_body["from alice's phone"]["sender"]["person_id"] == "PCID-ALICE"
    assert by_body["from alice's email"]["sender"]["person_id"] == "PCID-ALICE"
    assert by_body["from bob"]["sender"]["person_id"] == "PCID-BOB"
