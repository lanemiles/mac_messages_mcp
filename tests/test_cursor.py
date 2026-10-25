# Copyright (c) 2023 Carter Lasalle
"""The ROWID cursor contract, exercised against a real-schema chat.db.

``since_rowid`` and ``wait_for_new_messages`` both ask the caller for "the
highest ROWID you have seen", so a read must render that ROWID and a wait must
only wake for rows inside its own contact/chat scope. These tests use the
schema-faithful ``chat_db`` fixture (``tests/chatdb.py``) rather than mocking
``query_messages_db``: the wait bug lives in how the global MAX(ROWID) probe
interacts with the scoped read, which a single mocked return value cannot
express.
"""

from __future__ import annotations

import re

import pytest

from mac_messages_mcp import messages
from mac_messages_mcp.messages import (
    fuzzy_search_messages,
    get_recent_messages,
    wait_for_new_messages,
)
from mac_messages_mcp.untrusted import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

ALICE = "+15555550100"
BOB = "+15555550199"


@pytest.fixture
def people(chat_db):
    """Two handles (ROWIDs 1 and 2) so tests can address Alice and Bob."""
    return {"alice": chat_db.add_handle(ALICE), "bob": chat_db.add_handle(BOB)}


def _add(chat_db, rowid, handle_id, text, *, is_from_me=0):
    return chat_db.add_message(
        text, rowid=rowid, handle_id=handle_id, is_from_me=is_from_me
    )


@pytest.fixture
def fake_clock(monkeypatch):
    """Make ``time.sleep`` advance ``time.monotonic`` instead of blocking."""
    state = {"now": 0.0, "sleeps": []}

    def monotonic() -> float:
        return state["now"]

    def sleep(seconds: float) -> None:
        state["sleeps"].append(seconds)
        state["now"] += seconds

    monkeypatch.setattr(messages.time, "monotonic", monotonic)
    monkeypatch.setattr(messages.time, "sleep", sleep)
    return state


def _inner(fenced: str) -> str:
    assert fenced.startswith(UNTRUSTED_OPEN) and fenced.endswith(UNTRUSTED_CLOSE)
    return fenced[len(UNTRUSTED_OPEN) : -len(UNTRUSTED_CLOSE)].strip()


def _message_lines(fenced: str) -> list[str]:
    # The boundary escapes structural newlines to the two characters ``\\n`` so
    # untrusted text cannot forge a transcript line; undo that to count lines.
    logical = _inner(fenced).replace("\\n", "\n").splitlines()
    return [line for line in logical if line.startswith("[")]


# ---------------------------------------------------------------------------
# A read must expose the ROWID the cursor API asks for
# ---------------------------------------------------------------------------


def test_recent_messages_render_each_rowid(chat_db, people):
    _add(chat_db, 4512, 1, "running late")
    _add(chat_db, 4513, 1, "no worries", is_from_me=1)

    result = get_recent_messages(hours=1)

    lines = _message_lines(result)
    assert len(lines) == 2
    # The ROWID follows the timestamp so an agent can read it off every line.
    assert re.search(r"^\[[^\]]+\] #4513 You", lines[0]), lines[0]
    assert re.search(r"^\[[^\]]+\] #4512 ", lines[1]), lines[1]
    assert "running late" in lines[1]


def test_fuzzy_search_renders_each_rowid(chat_db, people):
    _add(chat_db, 77, 1, "dinner at seven?")

    result = fuzzy_search_messages("dinner", hours=1)

    lines = _message_lines(result)
    assert len(lines) == 1
    assert re.search(r"^\[[^\]]+\] #77 ", lines[0]), lines[0]
    assert "(Score: 1.00)" in lines[0]


def test_cursor_read_states_the_next_cursor(chat_db, people):
    _add(chat_db, 10, 1, "first")
    _add(chat_db, 11, 1, "second")
    _add(chat_db, 12, 2, "third")

    result = get_recent_messages(since_rowid=10)

    inner = _inner(result)
    assert "#11 " in inner and "#12 " in inner
    assert "#10 " not in inner
    assert "since_rowid=12" in inner


def test_cursor_read_advances_past_rows_with_no_body(chat_db, people):
    # A row with neither text nor attributedBody renders nothing, but it still
    # consumes the cursor: reporting "no new messages" would make the caller
    # re-read the same row forever.
    _add(chat_db, 20, 1, None)
    _add(chat_db, 21, 1, None)

    result = get_recent_messages(since_rowid=19)

    inner = _inner(result)
    assert "No new messages since ROWID 19." not in inner
    assert "since_rowid=21" in inner


def test_cursor_read_with_nothing_newer_keeps_the_old_wording(chat_db, people):
    _add(chat_db, 30, 1, "old")
    assert "No new messages since ROWID 30." in _inner(
        get_recent_messages(since_rowid=30)
    )


# ---------------------------------------------------------------------------
# A scoped wait must only wake for rows inside its scope
# ---------------------------------------------------------------------------


def test_wait_scoped_to_a_contact_ignores_other_threads(chat_db, people, fake_clock):
    _add(chat_db, 5, 1, "from alice")
    # Newer row, but from Bob: outside the Alice-scoped wait.
    _add(chat_db, 6, 2, "from bob")

    result = wait_for_new_messages(
        since_rowid=5, contact=ALICE, timeout_seconds=3, poll_interval=1
    )

    inner = _inner(result)
    assert "No new messages arrived within 3 seconds" in inner, inner
    assert "from bob" not in inner
    # The wait actually waited instead of returning on Bob's row.
    assert fake_clock["sleeps"], "wait returned without ever sleeping"
    assert fake_clock["now"] >= 3


def test_wait_scoped_to_a_contact_does_not_hot_loop(chat_db, people, fake_clock):
    # Regression: with an out-of-scope newer row present, every call used to
    # return instantly with "No new messages since ROWID N", and since the
    # caller could not advance N, a polling loop spun without sleeping.
    _add(chat_db, 5, 1, "from alice")
    _add(chat_db, 6, 2, "from bob")

    first = _inner(
        wait_for_new_messages(
            since_rowid=5, contact=ALICE, timeout_seconds=2, poll_interval=1
        )
    )
    assert "No new messages since ROWID 5." not in first, first
    assert len(fake_clock["sleeps"]) >= 2, fake_clock


def test_wait_scoped_to_a_contact_returns_that_contacts_row(
    chat_db, people, fake_clock
):
    _add(chat_db, 5, 1, "from alice")
    _add(chat_db, 6, 2, "from bob")
    _add(chat_db, 7, 1, "alice again")

    result = wait_for_new_messages(
        since_rowid=5, contact=ALICE, timeout_seconds=3, poll_interval=1
    )

    inner = _inner(result)
    assert "#7 " in inner and "alice again" in inner
    assert "from bob" not in inner
    assert "since_rowid=7" in inner
    assert not fake_clock["sleeps"]


def test_wait_fails_fast_on_an_unresolvable_scope(chat_db, people, fake_clock):
    result = wait_for_new_messages(
        since_rowid=0, contact="+15550000000", timeout_seconds=5
    )
    inner = _inner(result)
    assert "No message history found" in inner or "Could not find" in inner, inner
    assert not fake_clock["sleeps"], "scope errors should not wait out the timeout"


def test_wait_unscoped_still_wakes_for_any_thread(chat_db, people, fake_clock):
    _add(chat_db, 5, 1, "from alice")
    _add(chat_db, 6, 2, "from bob")

    inner = _inner(wait_for_new_messages(since_rowid=5, timeout_seconds=3))
    assert "#6 " in inner and "from bob" in inner
    assert not fake_clock["sleeps"]


# ---------------------------------------------------------------------------
# The ROWID survives the MCP presentation layer
# ---------------------------------------------------------------------------


def test_tool_output_carries_the_rowid_through_the_fence(chat_db, people):
    from mac_messages_mcp.server import tool_get_recent_messages

    _add(chat_db, 900, 1, "hi")
    fenced = tool_get_recent_messages(hours=1)
    assert "#900 " in _inner(fenced)
