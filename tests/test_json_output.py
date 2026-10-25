# Copyright (c) 2023 Carter Lasalle
"""``format="json"``: machine-readable output, and local-day date filters.

Prose output forces an agent to parse dates, senders, and IDs out of lines
whose delimiters (``:``, ``[``, ``]``) message bodies can also contain. JSON
gives every field its own slot. Date filters also used to be UTC days while
timestamps displayed in local time, so "messages from the 15th" missed the
evening of the 15th in any timezone behind UTC.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from mac_messages_mcp import db, read
from mac_messages_mcp.messages import (
    fuzzy_search_messages,
    get_recent_messages,
    list_conversations,
    search_attachments,
    wait_for_new_messages,
)
from mac_messages_mcp.untrusted import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

from .chatdb import apple_ns

ALICE = "+15555550100"


def _inner(fenced: str) -> str:
    assert fenced.startswith(UNTRUSTED_OPEN) and fenced.endswith(UNTRUSTED_CLOSE)
    return fenced[len(UNTRUSTED_OPEN) : -len(UNTRUSTED_CLOSE)].strip()


def _json(fenced: str):
    inner = _inner(fenced)
    try:
        return json.loads(inner)
    except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic
        pytest.fail(f"not JSON ({exc}): {inner[:200]}")


@pytest.fixture
def eastern(monkeypatch):
    """Pin the Mac's local timezone to UTC-5 for both filters and display."""
    tz = timezone(timedelta(hours=-5), name="EST")
    monkeypatch.setattr(db, "local_timezone", lambda: tz)
    return tz


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_recent_messages_json_has_one_field_per_fact(chat_db, tmp_path):
    alice = chat_db.add_handle(ALICE)
    chat = chat_db.add_chat(ALICE, style=45, handles=(alice,))
    tricky = "line one\nline two: [not a tag] <b>"
    msg = chat_db.add_message(tricky, handle_id=alice, chat_id=chat, is_read=0)
    picture = tmp_path / "IMG_9.png"
    picture.write_bytes(b"\x89PNG\r\n")
    att = chat_db.add_attachment(msg, str(picture), "image/png")
    mine = chat_db.add_message("ok", handle_id=alice, is_from_me=1, chat_id=chat)

    data = _json(get_recent_messages(hours=1, format="json"))

    assert set(data) >= {"messages", "cursor", "page"}
    assert [m["rowid"] for m in data["messages"]] == [mine, msg]
    first = data["messages"][1]
    assert first["body"] == tricky, "body round-trips exactly, newline included"
    assert first["is_from_me"] is False
    assert first["sender"]["handle_id"] == alice
    assert first["sender"]["address"] == ALICE
    assert first["service"] == "iMessage"
    assert "unread" in first["tags"]
    assert first["attachments"] == [
        {
            "id": att,
            "mime_type": "image/png",
            "filename": "IMG_9.png",
            "size_bytes": 6,
            "exists": True,
            "downloaded": True,
            "genmoji": None,
            "path": str(picture),
        }
    ]
    # ISO 8601 with an explicit offset, not a bare local string.
    datetime.fromisoformat(first["date"])
    assert first["date"][-6] in "+-" or first["date"].endswith("Z")
    assert data["cursor"]["next_since_rowid"] == mine
    assert data["page"] == {"limit": 100, "offset": 0, "has_more": False}


def test_json_rejects_unknown_format(chat_db):
    out = _inner(get_recent_messages(hours=1, format="xml"))
    assert out.startswith("Error") and "format" in out


def test_fuzzy_search_json_carries_scores(chat_db):
    alice = chat_db.add_handle(ALICE)
    rowid = chat_db.add_message("dinner at seven?", handle_id=alice)
    data = _json(fuzzy_search_messages("dinner", hours=1, format="json"))
    assert data["query"] == "dinner"
    assert data["messages"][0]["rowid"] == rowid
    assert data["messages"][0]["score"] == 1.0


def test_list_conversations_json(chat_db):
    alice = chat_db.add_handle(ALICE)
    group = chat_db.add_chat("chat1", style=43, display_name="Family", handles=(alice,))
    chat_db.add_message("hi", handle_id=alice, chat_id=group, is_read=0)
    data = _json(list_conversations(format="json"))
    assert data["conversations"][0] == {
        "chat_id": "chat1",
        "name": "Family",
        "kind": "group",
        "participants": None,
        "archived": False,
        "filtered": False,
        "message_count": 1,
        "unread_count": 1,
        "unseen_mentions": 0,
        "unseen_since_last_opened": None,
        "last_opened_at": None,
        "last_message_at": data["conversations"][0]["last_message_at"],
    }
    datetime.fromisoformat(data["conversations"][0]["last_message_at"])


def test_search_attachments_json(chat_db, tmp_path):
    alice = chat_db.add_handle(ALICE)
    f = tmp_path / "notes.txt"
    f.write_text("x")
    msg = chat_db.add_message("see file", handle_id=alice)
    att = chat_db.add_attachment(msg, str(f), "text/plain")
    data = _json(search_attachments(format="json"))
    item = data["attachments"][0]
    assert item["id"] == att and item["message_id"] == msg
    assert item["mime_type"] == "text/plain" and item["exists"] is True
    assert item["sender"]["address"] == ALICE


def test_wait_for_new_messages_can_return_json(chat_db, monkeypatch):
    alice = chat_db.add_handle(ALICE)
    rowid = chat_db.add_message("new", handle_id=alice)
    monkeypatch.setattr(read.time, "sleep", lambda s: None)
    data = _json(
        wait_for_new_messages(since_rowid=rowid - 1, timeout_seconds=1, format="json")
    )
    assert data["messages"][0]["rowid"] == rowid
    assert data["cursor"]["next_since_rowid"] == rowid


def test_tool_level_format_parameter(chat_db):
    from mac_messages_mcp.server import tool_get_recent_messages

    alice = chat_db.add_handle(ALICE)
    chat_db.add_message("via tool", handle_id=alice)
    data = _json(tool_get_recent_messages(hours=1, format="json"))
    assert data["messages"][0]["body"] == "via tool"


# ---------------------------------------------------------------------------
# Timezone: filters and display agree on what "the 15th" means
# ---------------------------------------------------------------------------


def test_date_filters_use_the_local_day_not_utc(chat_db, eastern):
    alice = chat_db.add_handle(ALICE)
    # 23:30 on Jan 15 in UTC-5 is 04:30 UTC on Jan 16.
    late_local = datetime(2026, 1, 15, 23, 30, tzinfo=eastern)
    rowid = chat_db.add_message(
        "late night", handle_id=alice, date=apple_ns(late_local)
    )
    early_next = datetime(2026, 1, 16, 0, 30, tzinfo=eastern)
    chat_db.add_message("after midnight", handle_id=alice, date=apple_ns(early_next))

    the_15th = _inner(
        get_recent_messages(start_date="2026-01-15", end_date="2026-01-15")
    )
    assert "late night" in the_15th
    assert "after midnight" not in the_15th
    assert "[2026-01-15 23:30:00]" in the_15th, "display is local too"

    data = _json(
        get_recent_messages(
            start_date="2026-01-15", end_date="2026-01-15", format="json"
        )
    )
    assert data["messages"][0]["rowid"] == rowid
    assert data["messages"][0]["date"] == "2026-01-15T23:30:00-05:00"
