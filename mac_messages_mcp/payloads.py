# Copyright (c) 2023 Carter Lasalle
"""Decode the binary plists chat.db keeps beside a message.

``message.payload_data`` carries an iMessage app balloon's content as an
NSKeyedArchiver plist: a link preview's title and URL, an Apple Cash amount, a
poll's options. ``message.message_summary_info`` carries an edited message's
history: per message part, a list of ``{"d": seconds since 2001, "t":
typedstream}`` entries. Apple does not document either, so decoding is
best-effort and every failure returns None rather than raising.
"""

from __future__ import annotations

import plistlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .typedstream import extract_body_from_attributed

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
# Dotted identifiers (com.apple.Foo, NSMutableString, class paths) and
# keyed-archiver keys ($null, $class) are structure, not content.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][\w-]*(\.[A-Za-z_][\w-]*)+$|^NS[A-Z]\w*$")
_ARCHIVER_KEY_RE = re.compile(r"^\$[A-Za-z]+$")
_URL_BALLOON = "com.apple.messages.URLBalloonProvider"
_MAX_DETAILS = 6
_MAX_DETAIL_CHARS = 120


def _load_plist(blob: Any) -> Any:
    if not blob or not isinstance(blob, (bytes, bytearray)):
        return None
    try:
        return plistlib.loads(bytes(blob))
    except Exception:  # plistlib raises several unrelated types on bad input
        return None


def _archive_strings(plist: Any) -> list[str]:
    """Human-readable strings from an NSKeyedArchiver ``$objects`` table, in order.

    Class descriptors and the ``$null`` sentinel are skipped, as are strings
    that look like identifiers rather than content.
    """
    objects = plist.get("$objects") if isinstance(plist, dict) else None
    if not isinstance(objects, list):
        return []
    strings: list[str] = []
    for obj in objects:
        if isinstance(obj, dict):
            value = obj.get("NS.string")
            if isinstance(value, str):
                obj = value
            else:
                continue
        if not isinstance(obj, str) or obj == "$null":
            continue
        text = obj.strip()
        if not text or _IDENTIFIER_RE.match(text) or _ARCHIVER_KEY_RE.match(text):
            continue
        if text not in strings:
            strings.append(text[:_MAX_DETAIL_CHARS])
    return strings


def decode_balloon(blob: Any, bundle_id: Any) -> dict[str, Any] | None:
    """Describe an app balloon's payload, or None when there is nothing readable.

    Returns ``{"kind": "link"|"app", "title", "url", "details": [...]}``. For a
    link preview, ``title`` and ``url`` are picked out; for other apps the
    readable strings are offered as ``details``.
    """
    plist = _load_plist(blob)
    if plist is None:
        return None
    strings = _archive_strings(plist)
    if not strings:
        return None
    url = next((s for s in strings if s.startswith(("http://", "https://"))), None)
    title = next((s for s in strings if s != url), None)
    kind = "link" if str(bundle_id or "") == _URL_BALLOON or url else "app"
    return {
        "kind": kind,
        "title": title,
        "url": url,
        "details": strings[:_MAX_DETAILS],
    }


def _edit_time(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    seconds = number / 1_000_000_000 if number > 1e12 else number
    try:
        return (_APPLE_EPOCH + timedelta(seconds=seconds)).isoformat()
    except (OverflowError, ValueError):
        return None


def decode_summary_info(blob: Any) -> dict[str, Any] | None:
    """Edit history from ``message_summary_info``, or None.

    Returns ``{"edits": [{"at": iso-8601 UTC, "body": text}, ...]}`` in order,
    oldest first, across all parts of the message. ``edits`` is never empty.
    """
    plist = _load_plist(blob)
    if not isinstance(plist, dict):
        return None
    parts = plist.get("ec")
    if not isinstance(parts, dict):
        return None
    edits: list[dict[str, Any]] = []
    for _part, entries in sorted(parts.items(), key=lambda kv: str(kv[0])):
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            body = extract_body_from_attributed(entry.get("t"))
            if not body:
                continue
            edits.append({"at": _edit_time(entry.get("d")), "body": body})
    return {"edits": edits} if edits else None
