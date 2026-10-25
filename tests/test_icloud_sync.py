# Copyright (c) 2023 Carter Lasalle
"""Reads against a chat.db populated by Messages in iCloud.

A Mac that joins Messages in iCloud (or restores from a backup) downloads its
history in whatever order the server hands it out, so ``message.ROWID`` is the
*download* order, not the send order: a 2018 text can sit at a higher ROWID
than yesterday's. And a message the user sent from another device arrives with
``handle_id = 0``. Both were observed on a freshly set-up Mac mini (2026-10-25)
and both broke reads that had assumed ROWID == chronology and
``handle_id == the other party``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from mac_messages_mcp.messages import (
    fuzzy_search_messages,
    get_conversation,
    get_recent_messages,
)
from mac_messages_mcp.untrusted import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

from .chatdb import apple_ns

ALICE = "+15555550100"
BOB = "+15555550199"


def _inner(fenced: str) -> str:
    assert fenced.startswith(UNTRUSTED_OPEN) and fenced.endswith(UNTRUSTED_CLOSE)
    return (
        fenced[len(UNTRUSTED_OPEN) : -len(UNTRUSTED_CLOSE)].strip().replace("\\n", "\n")
    )


def _bodies(fenced: str) -> list[str]:
    return [
        line.split(": ", 1)[1]
        for line in _inner(fenced).splitlines()
        if line.startswith("[20")
    ]


def _minutes_ago(n: int) -> int:
    return apple_ns(datetime.now(timezone.utc) - timedelta(minutes=n))


# ---------------------------------------------------------------------------
# Contact filter: the user's own side of a 1:1 thread has handle_id = 0
# ---------------------------------------------------------------------------


@pytest.fixture
def synced_direct(chat_db, address_book):
    """Alice's 1:1 thread as iCloud leaves it: inbound rows carry her handle,
    the user's outbound rows carry handle_id 0. Plus a group with Alice in it."""
    address_book.add_contact("Alice", "Example", phones=(ALICE,))
    alice = chat_db.add_handle(ALICE)
    bob = chat_db.add_handle(BOB)
    direct = chat_db.add_chat(ALICE, style=45, handles=(alice,))
    group = chat_db.add_chat(
        "chat777", style=43, display_name="Trio", handles=(alice, bob)
    )
    chat_db.add_message(
        "alice: hi", handle_id=alice, chat_id=direct, date=_minutes_ago(30)
    )
    chat_db.add_message(
        "me: hi back (synced)",
        handle_id=0,
        is_from_me=1,
        chat_id=direct,
        date=_minutes_ago(20),
    )
    chat_db.add_message(
        "alice in group", handle_id=alice, chat_id=group, date=_minutes_ago(10)
    )
    chat_db.add_message(
        "me in group (synced)",
        handle_id=0,
        is_from_me=1,
        chat_id=group,
        date=_minutes_ago(5),
    )
    return {"alice": alice, "direct": direct, "group": group}


def test_contact_read_includes_my_synced_replies(chat_db, synced_direct):
    bodies = _bodies(get_recent_messages(hours=1, contact=ALICE))
    assert "me: hi back (synced)" in bodies, bodies
    assert "alice: hi" in bodies


def test_contact_read_by_name_includes_my_synced_replies(chat_db, synced_direct):
    bodies = _bodies(get_recent_messages(hours=1, contact="Alice"))
    assert "me: hi back (synced)" in bodies, bodies


def test_contact_read_does_not_pull_my_group_messages(chat_db, synced_direct):
    """Alice's own lines in a group still match (she sent them); mine in that
    group do not — a group thread is not a conversation *with* Alice."""
    bodies = _bodies(get_recent_messages(hours=1, contact=ALICE))
    assert "alice in group" in bodies
    assert "me in group (synced)" not in bodies, bodies


def test_contact_scoped_search_includes_my_synced_replies(chat_db, synced_direct):
    out = _inner(fuzzy_search_messages("hi back", hours=1, contact=ALICE))
    assert "me: hi back (synced)" in out, out


# ---------------------------------------------------------------------------
# get_conversation: ROWID is download order, not send order
# ---------------------------------------------------------------------------


@pytest.fixture
def scrambled(chat_db, address_book):
    """Six messages whose ROWIDs run opposite to their dates, as iCloud left
    them. ``order`` is the chronological list of bodies."""
    address_book.add_contact("Alice", "Example", phones=(ALICE,))
    alice = chat_db.add_handle(ALICE)
    direct = chat_db.add_chat(ALICE, style=45, handles=(alice,))
    ids: dict[str, int] = {}
    with chat_db.batch():
        for i in range(6):
            body = f"msg {i}"
            ids[body] = chat_db.add_message(
                body,
                handle_id=alice,
                chat_id=direct,
                rowid=1000 - i,  # oldest message has the HIGHEST rowid
                date=_minutes_ago(60 - i * 10),
            )
    return {"ids": ids, "order": [f"msg {i}" for i in range(6)]}


def test_default_page_is_newest_by_date_not_by_rowid(chat_db, scrambled):
    page = get_conversation(ALICE, limit=3)
    assert _bodies(page) == ["msg 3", "msg 4", "msg 5"], _inner(page)
    ids = scrambled["ids"]
    assert f"before_rowid={ids['msg 3']}" in _inner(page)
    assert f"after_rowid={ids['msg 5']}" in _inner(page)


def test_paging_back_and_forward_follows_dates(chat_db, scrambled):
    ids = scrambled["ids"]
    older = get_conversation(ALICE, limit=3, before_rowid=ids["msg 3"])
    assert _bodies(older) == ["msg 0", "msg 1", "msg 2"], _inner(older)
    assert "before_rowid=" not in _inner(older), "start of thread reached"

    newer = get_conversation(ALICE, limit=2, after_rowid=ids["msg 2"])
    assert _bodies(newer) == ["msg 3", "msg 4"], _inner(newer)
    assert f"after_rowid={ids['msg 4']}" in _inner(newer)


def test_json_cursors_are_the_boundary_messages_by_date(chat_db, scrambled):
    ids = scrambled["ids"]
    data = json.loads(_inner(get_conversation(ALICE, limit=2, format="json")))
    assert [m["body"] for m in data["messages"]] == ["msg 4", "msg 5"]
    assert data["cursors"] == {
        "before_rowid": ids["msg 4"],
        "after_rowid": ids["msg 5"],
        "has_older": True,
    }


def test_unknown_cursor_is_an_error_not_an_empty_page(chat_db, scrambled):
    out = get_conversation(ALICE, before_rowid=424242)
    assert "Error" in out and "424242" in out


# ---------------------------------------------------------------------------
# [not delivered]: synced outbound rows never get a receipt on this device
# ---------------------------------------------------------------------------


def test_synced_outbound_without_receipt_is_not_flagged_undelivered(
    chat_db, synced_direct
):
    """A message the user sent from the iPhone arrives with is_sent=1,
    is_delivered=0 and no receipt dates — that is a sync artifact, not a
    failure. Only a row that Messages marked with an error is undelivered."""
    direct = synced_direct["direct"]
    chat_db.add_message(
        "sent from my phone",
        handle_id=0,
        is_from_me=1,
        is_sent=1,
        is_delivered=0,
        error=0,
        chat_id=direct,
        date=_minutes_ago(3),
    )
    chat_db.add_message(
        "really failed",
        handle_id=0,
        is_from_me=1,
        is_sent=1,
        is_delivered=0,
        error=22,
        service="SMS",
        chat_id=direct,
        date=_minutes_ago(2),
    )
    lines = _inner(get_recent_messages(hours=1, chat_id=ALICE)).splitlines()
    phone = next(l for l in lines if "sent from my phone" in l)
    failed = next(l for l in lines if "really failed" in l)
    assert "[not delivered]" not in phone, phone
    assert "[not delivered]" in failed, failed


def test_json_exposes_the_error_code(chat_db, synced_direct):
    chat_db.add_message(
        "really failed",
        handle_id=0,
        is_from_me=1,
        is_sent=1,
        is_delivered=0,
        error=22,
        chat_id=synced_direct["direct"],
        date=_minutes_ago(2),
    )
    data = json.loads(
        _inner(get_recent_messages(hours=1, chat_id=ALICE, format="json"))
    )
    failed = next(m for m in data["messages"] if m["body"] == "really failed")
    assert failed["error_code"] == 22
    ok = next(m for m in data["messages"] if m["body"] == "me: hi back (synced)")
    assert ok.get("error_code") in (None, 0)
