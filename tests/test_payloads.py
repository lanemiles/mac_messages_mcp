# Copyright (c) 2023 Carter Lasalle
"""Decoding the binary plists chat.db keeps beside a message.

``payload_data`` holds an iMessage app balloon's content (a link preview's
title and URL, an Apple Cash amount, a poll's options); ``message_summary_info``
holds an edited message's history. Both used to render as a bare tag.
"""

from __future__ import annotations

import json
import plistlib
from datetime import datetime, timedelta, timezone

import pytest

from mac_messages_mcp import db, payloads
from mac_messages_mcp.messages import get_recent_messages
from mac_messages_mcp.untrusted import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

from .chatdb import apple_ns, make_attributed_body

ALICE = "+15555550100"
URL_BALLOON = "com.apple.messages.URLBalloonProvider"
CASH_BALLOON = "com.apple.PassbookUIService.PeerPaymentMessagesExtension"


def _inner(fenced: str) -> str:
    assert fenced.startswith(UNTRUSTED_OPEN) and fenced.endswith(UNTRUSTED_CLOSE)
    return (
        fenced[len(UNTRUSTED_OPEN) : -len(UNTRUSTED_CLOSE)].strip().replace("\\n", "\n")
    )


def _keyed_archive(*strings: str) -> bytes:
    """An NSKeyedArchiver plist whose $objects carry the given strings."""
    objects = [
        "$null",
        {"title": plistlib.UID(2), "URL": plistlib.UID(3)},
        *strings,
        {
            "$classname": "RichLinkMetadata",
            "$classes": ["RichLinkMetadata", "NSObject"],
        },
    ]
    return plistlib.dumps(
        {
            "$archiver": "NSKeyedArchiver",
            "$version": 100000,
            "$top": {"root": plistlib.UID(1)},
            "$objects": objects,
        },
        fmt=plistlib.FMT_BINARY,
    )


def _summary_info(edits: list[tuple[datetime, str]]) -> bytes:
    """message_summary_info for one edited part: [{d: seconds since 2001, t: typedstream}]."""
    epoch = datetime(2001, 1, 1, tzinfo=timezone.utc)
    return plistlib.dumps(
        {
            "ec": {
                "0": [
                    {
                        "d": (when - epoch).total_seconds(),
                        "t": make_attributed_body(text),
                    }
                    for when, text in edits
                ]
            }
        },
        fmt=plistlib.FMT_BINARY,
    )


def test_edit_history_is_decoded(chat_db):
    alice = chat_db.add_handle(ALICE)
    t0 = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)
    chat_db.add_message(
        "see you at 8",
        handle_id=alice,
        date=apple_ns(t0),
        date_edited=apple_ns(t0 + timedelta(minutes=3)),
        message_summary_info=_summary_info(
            [(t0, "see you at 7"), (t0 + timedelta(minutes=3), "see you at 8")]
        ),
    )
    out = _inner(get_recent_messages(start_date="2026-04-01", end_date="2026-04-01"))
    assert '[edited; was: "see you at 7"]: see you at 8' in out

    data = json.loads(
        _inner(
            get_recent_messages(
                start_date="2026-04-01", end_date="2026-04-01", format="json"
            )
        )
    )
    history = data["messages"][0]["edit_history"]
    assert [h["body"] for h in history] == ["see you at 7", "see you at 8"]
    assert history[0]["at"].startswith("2026-04-01T09:00")


def test_link_preview_title_and_url_are_surfaced(chat_db):
    alice = chat_db.add_handle(ALICE)
    chat_db.add_message(
        "https://example.com/story",
        handle_id=alice,
        balloon_bundle_id=URL_BALLOON,
        payload_data=_keyed_archive(
            "How to brew better coffee", "https://example.com/story"
        ),
    )
    out = _inner(get_recent_messages(hours=1))
    assert (
        '[link preview: "How to brew better coffee"]: https://example.com/story' in out
    )
    data = json.loads(_inner(get_recent_messages(hours=1, format="json")))
    assert data["messages"][0]["link"] == {
        "title": "How to brew better coffee",
        "url": "https://example.com/story",
    }


def test_app_balloon_details_are_surfaced(chat_db):
    alice = chat_db.add_handle(ALICE)
    chat_db.add_message(
        None,
        handle_id=alice,
        balloon_bundle_id=CASH_BALLOON,
        attributedBody=make_attributed_body("￼"),
        payload_data=_keyed_archive("$25.00", "Dinner split"),
    )
    out = _inner(get_recent_messages(hours=1))
    assert "[app: PeerPaymentMessagesExtension — $25.00; Dinner split]" in out
    data = json.loads(_inner(get_recent_messages(hours=1, format="json")))
    assert data["messages"][0]["app_payload"]["details"] == ["$25.00", "Dinner split"]
    assert data["messages"][0]["app_payload"]["app"] == "PeerPaymentMessagesExtension"


def test_garbage_payloads_do_not_break_the_read(chat_db):
    alice = chat_db.add_handle(ALICE)
    chat_db.add_message(
        "ok",
        handle_id=alice,
        balloon_bundle_id=URL_BALLOON,
        payload_data=b"\x00not a plist",
    )
    chat_db.add_message(
        "fine", handle_id=alice, date_edited=apple_ns(), message_summary_info=b"garbage"
    )
    out = _inner(get_recent_messages(hours=1))
    assert "[link preview]: ok" in out
    assert "[edited]: fine" in out


def test_payload_blobs_are_fetched_only_for_rows_that_need_them(chat_db, monkeypatch):
    alice = chat_db.add_handle(ALICE)
    for i in range(5):
        chat_db.add_message(f"plain {i}", handle_id=alice)
    queries: list[str] = []
    real = db.query_messages_db

    def spy(query, params=()):
        queries.append(" ".join(query.split()))
        return real(query, params)

    monkeypatch.setattr(db, "query_messages_db", spy)
    get_recent_messages(hours=1)
    assert not [
        q for q in queries if "payload_data" in q
    ], "no balloons or edits on this page"

    chat_db.add_message(
        "link",
        handle_id=alice,
        balloon_bundle_id=URL_BALLOON,
        payload_data=_keyed_archive("T", "https://x.y"),
    )
    queries.clear()
    get_recent_messages(hours=1)
    payload_queries = [q for q in queries if "payload_data" in q]
    assert len(payload_queries) == 1 and "ROWID IN (?)" in payload_queries[0]


def test_decoder_unit_behaviour():
    assert payloads.decode_balloon(None, URL_BALLOON) is None
    assert payloads.decode_balloon(b"junk", URL_BALLOON) is None
    link = payloads.decode_balloon(
        _keyed_archive("Title", "https://a.b/c"), URL_BALLOON
    )
    assert link == {
        "kind": "link",
        "title": "Title",
        "url": "https://a.b/c",
        "details": ["Title", "https://a.b/c"],
    }
    assert payloads.decode_summary_info(b"") is None
