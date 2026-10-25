# Copyright (c) 2023 Carter Lasalle
"""Reading messages: body decoding, scope resolution, recent-message reads,
conversation listing, waiting for new rows, and fuzzy search."""

from __future__ import annotations

import json
import logging
import operator
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from thefuzz import fuzz

from . import attachments as _attachments
from . import chats as _chats
from . import contacts as _contacts
from . import db as _db
from . import output as _output
from . import search_index as _search_index
from .output import _MAX_MESSAGE_LIMIT
from .phone import canonical_handle, digits_only, is_email_handle
from .untrusted import bound_untrusted_output, neutralize_untrusted_text

logger = logging.getLogger(__name__)


def extract_body_from_attributed(attributed_body: bytes | None) -> str | None:
    """Extract message content from attributedBody binary data, or None.

    The typedstream layout the decoder walks is documented on
    ``_decode_attributed_body``.
    """
    try:
        return _decode_attributed_body(attributed_body)
    except (AttributeError, IndexError, TypeError, ValueError):
        return None


def _decode_attributed_body(attributed_body: bytes | None) -> str | None:
    r"""Decode the UTF-8 text out of an Apple typedstream blob.

    The attributedBody column contains an Apple typedstream blob
    (NSArchiver serialization of NSMutableAttributedString).  The string
    content is stored after the first ``NSString`` class marker followed
    by a 5-byte header (``\x01 <byte> \x84 \x01 +``) and a
    variable-length integer encoding the byte length of the UTF-8 text.

    Length encoding (first byte after the header):
        < 0x80  — the byte *is* the length.
        0x81    — next 2 bytes (little-endian) are the length.
        0x82    — next 3 bytes (little-endian) are the length.
        0x83    — next 4 bytes (little-endian) are the length.
    """
    if attributed_body is None:
        return None

    # Locate the first NSString class reference in the blob.
    marker = b"NSString"
    idx = attributed_body.find(marker)
    if idx < 0:
        return None

    # Skip past: NSString (8) + \x01 + <byte> + \x84 + \x01 + '+' = 5 bytes
    pos = idx + len(marker) + 5

    if pos >= len(attributed_body):
        return None

    # Read the variable-length integer for the text byte count.
    length_byte = attributed_body[pos]
    pos += 1

    if length_byte < 0x80:
        text_length = length_byte
    elif length_byte == 0x81:
        if pos + 2 > len(attributed_body):
            return None
        text_length = attributed_body[pos] | (attributed_body[pos + 1] << 8)
        pos += 2
    elif length_byte == 0x82:
        if pos + 3 > len(attributed_body):
            return None
        text_length = (
            attributed_body[pos]
            | (attributed_body[pos + 1] << 8)
            | (attributed_body[pos + 2] << 16)
        )
        pos += 3
    elif length_byte == 0x83:
        if pos + 4 > len(attributed_body):
            return None
        text_length = (
            attributed_body[pos]
            | (attributed_body[pos + 1] << 8)
            | (attributed_body[pos + 2] << 16)
            | (attributed_body[pos + 3] << 24)
        )
        pos += 4
    else:
        return None

    if pos + text_length > len(attributed_body):
        return None

    return attributed_body[pos : pos + text_length].decode(
        "utf-8",
        errors="replace",
    )


# Characters that stand in for an attachment inside a message body. Messages
# writes U+FFFC (OBJECT REPLACEMENT CHARACTER) into attributedBody where a file
# sits and omits it from the `text` column; U+FFFD is what a lossy decode of
# such a blob used to produce.
_ATTACHMENT_PLACEHOLDER_CHARS = frozenset("\ufffc\ufffd")


def _is_attachment_placeholder_body(body: str | None) -> bool:
    """Return True when a decoded body is nothing but attachment placeholders.

    An attachment-only message stores no words, only the object-replacement
    character standing in for the file, which is truthy but carries no
    content. Render those as [attachment] instead of a stray glyph.
    """
    if not body:
        return False
    stripped = body.strip()
    return bool(stripped) and all(
        ch in _ATTACHMENT_PLACEHOLDER_CHARS for ch in stripped
    )


def _strip_attachment_placeholders(body: str) -> str:
    """Drop U+FFFC placeholders from a body that also has words.

    This matches what Messages itself puts in the `text` column for a
    text-plus-attachment message, so rendered bodies and send verification
    compare like with like. The attachment summary names the files.
    """
    return body.replace("\ufffc", "")


_MAX_MESSAGE_BODY_CHARS = 4_000


# Apple's `message.associated_message_type` codes -> tapback names. The 2000
# range adds a reaction to the referenced message; the 3000 range removes one.
_TAPBACK_LABELS: dict[int, str] = {
    2000: "loved",
    2001: "liked",
    2002: "disliked",
    2003: "laughed",
    2004: "emphasized",
    2005: "questioned",
    2006: "emoji",
    3000: "removed loved",
    3001: "removed liked",
    3002: "removed disliked",
    3003: "removed laughed",
    3004: "removed emphasized",
    3005: "removed questioned",
    3006: "removed emoji",
}

# Columns every chat.db has; the read always selects these.
_BASE_MESSAGE_COLUMNS = (
    "ROWID",
    "guid",
    "date",
    "text",
    "attributedBody",
    "is_from_me",
    "handle_id",
    "cache_roomnames",
    "service",
    "is_read",
    "is_sent",
    "is_delivered",
    "associated_message_type",
    "thread_originator_guid",
)
# Columns added over the years; selected only when this database has them,
# so a read never fails on an older macOS for lack of a newer column.
_OPTIONAL_MESSAGE_COLUMNS = (
    "associated_message_guid",
    "associated_message_emoji",
    "date_edited",
    "date_retracted",
    "is_audio_message",
    "balloon_bundle_id",
    "item_type",
    "group_action_type",
    "group_title",
    "other_handle",
    "date_read",
    "date_delivered",
)
_MESSAGE_COLUMNS_CACHE: dict[str, str] = {}

_URL_BALLOON = "com.apple.messages.URLBalloonProvider"
_QUOTE_CHARS = 60


def _message_columns() -> str:
    """``m.col, m.col, ...`` for this database's message table."""
    db_path = _db.get_messages_db_path()
    cached = _MESSAGE_COLUMNS_CACHE.get(db_path)
    if cached is not None:
        return cached
    rows = _db.query_messages_db("PRAGMA table_info(message)")
    present = {str(r.get("name")) for r in rows if "name" in r} if rows else set()
    if "text" not in present:
        # Could not read the schema (or a mocked query returned message rows):
        # fall back to the base set, which every chat.db has.
        return ",\n        ".join(f"m.{c}" for c in _BASE_MESSAGE_COLUMNS)
    columns = _BASE_MESSAGE_COLUMNS + tuple(
        c for c in _OPTIONAL_MESSAGE_COLUMNS if c in present
    )
    sql = ",\n        ".join(f"m.{c}" for c in columns)
    _MESSAGE_COLUMNS_CACHE[db_path] = sql
    return sql


def _target_guid(value: Any) -> str | None:
    """The message guid an ``associated_message_guid`` points at.

    Stored as ``p:0/GUID`` (part index) or ``bp:GUID``; plain guids pass through.
    """
    if not value:
        return None
    text = str(value)
    if "/" in text:
        text = text.rsplit("/", 1)[1]
    elif text.startswith("bp:"):
        text = text[3:]
    return text or None


def _quote(body: str | None) -> str | None:
    if not body:
        return None
    flat = " ".join(str(body).split())
    if len(flat) > _QUOTE_CHARS:
        flat = flat[: _QUOTE_CHARS - 1].rstrip() + "…"
    return flat


def _quoted_bodies(guids: list[str]) -> dict[str, str]:
    """guid -> displayable body for the messages tapbacks and replies point at."""
    wanted = sorted({g for g in guids if g})
    if not wanted:
        return {}
    placeholders = ", ".join("?" for _ in wanted)
    rows = _db.query_messages_db(
        "SELECT guid, text, attributedBody, associated_message_type FROM message "
        f"WHERE guid IN ({placeholders})",
        tuple(wanted),
    )
    if not rows or "error" in rows[0]:
        return {}
    quotes: dict[str, str] = {}
    for row in rows:
        if "guid" not in row or row["guid"] not in wanted:
            continue
        body = _message_body(row)
        if body:
            quotes[str(row["guid"])] = body
    return quotes


def _balloon_label(bundle_id: Any) -> tuple[bool, str | None]:
    """(is_link_preview, app short name) for a balloon_bundle_id."""
    if not bundle_id:
        return False, None
    text = str(bundle_id)
    if text == _URL_BALLOON:
        return True, None
    return False, text.rsplit(".", 1)[-1] or text


def _group_event(row: dict[str, Any]) -> dict[str, Any] | None:
    """Describe a group-event row (rename, add/remove, leave), or None."""
    try:
        item_type = int(row.get("item_type") or 0)
    except (TypeError, ValueError):
        return None
    if item_type == 0:
        return None
    if item_type == 2:
        return {"type": "renamed", "title": row.get("group_title")}
    if item_type == 1:
        try:
            action = int(row.get("group_action_type") or 0)
        except (TypeError, ValueError):
            action = 0
        return {
            "type": "participant_removed" if action == 1 else "participant_added",
            "other_handle": row.get("other_handle"),
        }
    if item_type == 3:
        return {"type": "left"}
    return {"type": f"item_type_{item_type}"}


def _tapback_kind(row: dict[str, Any]) -> tuple[str, str | None] | None:
    """(kind label, emoji) for a tapback row, or None for an ordinary message."""
    associated = row.get("associated_message_type")
    if not associated:
        return None
    try:
        code = int(associated)
    except (TypeError, ValueError):
        return (f"reaction {associated}", None)
    label = _TAPBACK_LABELS.get(code, f"reaction {code}")
    emoji = row.get("associated_message_emoji") if code in (2006, 3006) else None
    return label, (str(emoji) if emoji else None)


def _clock_label(apple_ns: Any) -> str | None:
    try:
        moment = _db.from_apple_ns(int(apple_ns))
    except (TypeError, ValueError, OverflowError):
        return None
    return moment.astimezone(_db.local_timezone()).strftime("%H:%M")


def _message_tag_list(
    row: dict[str, Any],
    quotes: dict[str, str] | None = None,
    event: dict[str, Any] | None = None,
) -> list[str]:
    """Per-message annotations: service, receipts, tapback, reply, and more.

    Only keys present in ``row`` are rendered, so a row from an older chat.db
    schema (or a fixture that predates these columns) renders exactly as it did
    before: no tag, no change. ``quotes`` maps guids to the bodies tapbacks and
    replies point at.
    """
    quotes = quotes or {}
    tags: list[str] = []
    service = row.get("service")
    if service:
        tags.append(neutralize_untrusted_text(str(service)))
    if row.get("is_from_me"):
        read_at = _clock_label(row.get("date_read")) if row.get("date_read") else None
        delivered_at = (
            _clock_label(row.get("date_delivered"))
            if row.get("date_delivered")
            else None
        )
        if read_at:
            tags.append(f"read {read_at}")
        elif delivered_at:
            tags.append(f"delivered {delivered_at}")
        elif row.get("is_sent") and row.get("is_delivered") is not None:
            if not row.get("is_delivered"):
                tags.append("not delivered")
    elif row.get("is_read") is not None and not row.get("is_read"):
        tags.append("unread")
    if row.get("date_edited"):
        tags.append("edited")
    if row.get("date_retracted"):
        tags.append("unsent")
    if row.get("is_audio_message"):
        tags.append("audio message")
    is_link, app = _balloon_label(row.get("balloon_bundle_id"))
    if is_link:
        tags.append("link preview")
    elif app:
        tags.append(f"app: {neutralize_untrusted_text(app)}")
    if event is not None:
        tags.append("event")
    tapback = _tapback_kind(row)
    if tapback is not None:
        label, emoji = tapback
        shown = emoji if emoji else label
        target = _quote(
            quotes.get(_target_guid(row.get("associated_message_guid")) or "")
        )
        tag = f"tapback: {neutralize_untrusted_text(shown)}"
        if target:
            tag += f' → "{neutralize_untrusted_text(target)}"'
        tags.append(tag)
    originator = row.get("thread_originator_guid")
    if originator:
        target = _quote(quotes.get(str(originator)))
        tags.append(
            f'reply to: "{neutralize_untrusted_text(target)}"' if target else "reply"
        )
    return tags


def _message_metadata_tags(row: dict[str, Any]) -> str:
    """Compact ``[tag] [tag]`` suffix for a text line; empty when untagged."""
    tags = _message_tag_list(row)
    if not tags:
        return ""
    return " [" + "] [".join(tags) + "]"


def _message_body(row: dict[str, Any]) -> str | None:
    """Displayable body for a row, or None when the row has nothing to show."""
    if row.get("date_retracted"):
        return "(unsent message)"
    body = row.get("text")
    if not body and row.get("attributedBody"):
        body = extract_body_from_attributed(row["attributedBody"])
    if not body:
        if row.get("associated_message_type"):
            # A tapback row carries no body of its own: the reaction is the
            # content, and the tag names it.
            return "(reaction)"
        return None
    if _is_attachment_placeholder_body(body):
        return "[attachment]"
    return _strip_attachment_placeholders(body)


def _row_datetime(row: dict[str, Any]) -> datetime | None:
    try:
        return _db.from_apple_ns(int(row["date"]))
    except (KeyError, ValueError, TypeError, OverflowError) as e:
        logger.warning("Date conversion error: %s for timestamp %s", e, row.get("date"))
        return None


def _actor_name(row: dict[str, Any]) -> str:
    if row.get("is_from_me"):
        return "You"
    return _contacts.get_contact_name(row.get("handle_id"))


def _event_text(row: dict[str, Any], event: dict[str, Any]) -> str:
    actor = _actor_name(row)
    kind = event["type"]
    if kind == "renamed":
        title = event.get("title") or "(no name)"
        return f'{actor} named the group "{title}"'
    if kind in ("participant_added", "participant_removed"):
        other = event.get("other_handle")
        target = _contacts.get_contact_name(other) if other else "someone"
        verb = "added" if kind == "participant_added" else "removed"
        return f"{actor} {verb} {target}"
    if kind == "left":
        return f"{actor} left the group"
    return f"{actor}: group event ({kind})"


def _build_message_records(
    rows: list[dict[str, Any]],
    *,
    chat_mapping: dict[str, str],
    attachments_by_msg: dict[int, list[dict[str, Any]]],
    chat_display_name: str | None = None,
    chat_is_group: bool = False,
    scores: dict[int, float] | None = None,
) -> list[dict[str, Any]]:
    """Turn message rows into renderer-neutral records.

    Rows without displayable content are dropped (they still count toward the
    cursor, which callers compute from ``rows``). Sender names are resolved in
    one batch first so the per-row lookups hit the cache.
    """
    # Which thread each message belongs to, and who is in the unnamed groups.
    message_ids = [r["ROWID"] for r in rows if r.get("ROWID") is not None]
    chats = _chats._chats_for_message_ids(message_ids)
    unnamed_group_ids = sorted(
        {
            c["chat_row_id"]
            for c in chats.values()
            if c.get("style") == 43 and not c.get("display_name")
        }
    )
    participants = _chats._participants_for_chats(unnamed_group_ids)

    # Every sender, the counterpart on outbound 1:1 rows, and the subject of
    # any group event, in one batch.
    _contacts.prime_contact_names(
        h for row in rows for h in (row.get("handle_id"), row.get("other_handle")) if h
    )
    # Bodies of the messages tapbacks and replies point at, in one query.
    quotes = _quoted_bodies(
        [
            g
            for row in rows
            for g in (
                _target_guid(row.get("associated_message_guid")),
                row.get("thread_originator_guid"),
            )
            if g
        ]
    )
    records: list[dict[str, Any]] = []
    for row in rows:
        event = _group_event(row)
        body = _message_body(row)
        if body is None and event is not None:
            body = _event_text(row, event)
        if body is None:
            continue
        rowid = row.get("ROWID")
        chat = chats.get(rowid) if rowid is not None else None
        chat_is_group_row = chat is not None and chat.get("style") == 43
        chat_participants = (
            participants.get(chat["chat_row_id"]) if chat_is_group_row else None
        )
        roomnames = row.get("cache_roomnames")
        group_chat_name = chat_mapping.get(roomnames) if roomnames else None
        if not group_chat_name and chat_is_group_row and chat.get("display_name"):
            group_chat_name = chat["display_name"]
        if not group_chat_name and chat_display_name and chat_is_group:
            group_chat_name = chat_display_name
        if not group_chat_name and chat_is_group_row:
            group_chat_name = _chats._group_label(chat_participants or [])
        is_from_me = bool(row.get("is_from_me"))
        handle_id = row.get("handle_id")
        # On a 1:1 outbound row handle_id is the counterpart; only claim that
        # when the chat join confirms the thread is not a group.
        recipient: dict[str, Any] | None = None
        if is_from_me and handle_id and chat is not None and not chat_is_group_row:
            recipient = {
                "handle_id": handle_id,
                "address": _contacts.get_handle_address(handle_id),
                "name": _contacts.get_contact_name(handle_id),
            }
        if is_from_me:
            sender_name = (
                f"You → {neutralize_untrusted_text(recipient['name'])}"
                if recipient
                else "You"
            )
        else:
            sender_name = _contacts.get_contact_name(handle_id)
        records.append(
            {
                "rowid": rowid,
                "date": _row_datetime(row),
                "is_from_me": is_from_me,
                "handle_id": handle_id,
                "sender_name": sender_name,
                "recipient": recipient,
                "chat": chat,
                "chat_participants": chat_participants,
                "tags": _message_tag_list(row, quotes, event),
                "event": event,
                "quotes": quotes,
                "row": row,
                "group_chat_name": group_chat_name,
                "body": body,
                "attachments": attachments_by_msg.get(rowid, []) if rowid else [],
                "service": row.get("service"),
                "associated_message_type": row.get("associated_message_type"),
                "thread_originator_guid": row.get("thread_originator_guid"),
                "score": scores.get(rowid) if scores and rowid is not None else None,
            }
        )
    return records


def _render_message_line(record: dict[str, Any]) -> str:
    date = record["date"]
    date_str = _db.format_local(date) if date is not None else "Unknown date"
    prefix = _message_line_prefix(date_str, record["rowid"])
    if record["score"] is not None:
        prefix += f" (Score: {record['score']:.2f})"
    if record["group_chat_name"]:
        prefix += f" [{record['group_chat_name']}]"
    tags = record["tags"]
    metadata = " [" + "] [".join(tags) + "]" if tags else ""
    body = _sanitize_message_body(record["body"])
    attachment_summary = _attachments._format_attachment_summary(record["attachments"])
    return f"{prefix} {record['sender_name']}{metadata}: {body}{attachment_summary}"


def _attachment_json(att: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": att.get("id"),
        "mime_type": _output._json_text(att.get("mime_type")) or None,
        "filename": _output._json_text(att.get("filename")) or None,
        "size_bytes": att.get("size_bytes"),
        "exists": bool(att.get("exists")),
        "path": _output._json_text(att.get("path")) or None,
    }


def _message_json(record: dict[str, Any]) -> dict[str, Any]:
    date = record["date"]
    handle_id = record["handle_id"]
    sender: dict[str, Any] = {"name": _output._json_text(record["sender_name"])}
    if not record["is_from_me"]:
        sender["handle_id"] = handle_id
        sender["address"] = (
            _output._json_text(_contacts.get_handle_address(handle_id)) or None
        )
    item: dict[str, Any] = {
        "rowid": record["rowid"],
        "date": _db.isoformat_local(date) if date is not None else None,
        "is_from_me": record["is_from_me"],
        "sender": sender,
        "service": _output._json_text(record["service"]) or None,
        "body": _output._json_text(record["body"], max_chars=_MAX_MESSAGE_BODY_CHARS),
        "tags": [_output._json_text(tag) for tag in record["tags"]],
        "group_chat_name": _output._json_text(record["group_chat_name"]) or None,
        "chat": _chats._chat_json(record["chat"], record["chat_participants"]),
        "attachments": [_attachment_json(a) for a in record["attachments"]],
    }
    if record["recipient"] is not None:
        item["to"] = _output._participant_json(record["recipient"])
    row = record["row"]
    quotes = record["quotes"]
    originator = record["thread_originator_guid"]
    if originator:
        item["reply_to_guid"] = _output._json_text(originator)
        item["reply_to"] = {
            "guid": _output._json_text(originator),
            "body": _output._json_text(quotes.get(str(originator))) or None,
        }
    tapback = _tapback_kind(row)
    if tapback is not None:
        label, emoji = tapback
        target_guid = _target_guid(row.get("associated_message_guid"))
        item["tapback"] = {
            "kind": label,
            "emoji": emoji,
            "target": {
                "guid": _output._json_text(target_guid) or None,
                "body": _output._json_text(quotes.get(target_guid or "")) or None,
            },
        }
    item["edited_at"] = _iso_or_none(row.get("date_edited"))
    item["unsent_at"] = _iso_or_none(row.get("date_retracted"))
    item["is_audio"] = bool(row.get("is_audio_message"))
    is_link, _app = _balloon_label(row.get("balloon_bundle_id"))
    item["link_preview"] = is_link
    item["app_bundle"] = _output._json_text(row.get("balloon_bundle_id")) or None
    if record["is_from_me"]:
        item["delivered_at"] = _iso_or_none(row.get("date_delivered"))
        item["read_at"] = _iso_or_none(row.get("date_read"))
    if record["event"] is not None:
        event = dict(record["event"])
        event["actor"] = _output._json_text(_actor_name(row))
        other = event.pop("other_handle", None)
        if other:
            event["target"] = _output._participant_json(
                {
                    "handle_id": other,
                    "address": _contacts.get_handle_address(other),
                    "name": _contacts.get_contact_name(other),
                }
            )
        if "title" in event:
            event["title"] = _output._json_text(event["title"]) or None
        item["event"] = event
    if record["score"] is not None:
        item["score"] = record["score"]
    return item


def _iso_or_none(apple_ns: Any) -> str | None:
    if not apple_ns:
        return None
    try:
        return _db.isoformat_local(_db.from_apple_ns(int(apple_ns)))
    except (TypeError, ValueError, OverflowError):
        return None


def _sanitize_message_body(text: str, max_chars: int = _MAX_MESSAGE_BODY_CHARS) -> str:
    """Defense-in-depth field sanitizer for message bodies.

    The MCP security boundary is ``present_untrusted_output`` /
    ``bound_untrusted_output``. This helper still serializes a single body so
    a missed interpolation is less likely to introduce a structural line.
    """
    return neutralize_untrusted_text(text, max_chars=max_chars)


def _check_selection_index(index: int) -> str | None:
    """Return an error message when a contact:N index is invalid, else None."""
    if index < 0:
        return "Error: Contact selection must be a positive number (starting from 1)."
    return None


def _resolve_message_scope(
    contact: str | None,
    chat_id: str | None,
) -> tuple[list[int] | None, int | None, str | None, bool, str | None]:
    """Resolve an optional contact/chat filter into message-table scope.

    Returns ``(handle_ids, chat_row_id, chat_display_name, chat_is_group,
    error)``. ``error`` is a user-facing message when the filter cannot be
    resolved; otherwise at most one of the first two entries is populated.
    Shared by the recent-message read and the fuzzy search so both accept the
    same filters, including the ``contact:N`` selector.
    """
    if contact and chat_id:
        return (
            None,
            None,
            None,
            False,
            "Error: Provide either contact or chat_id, not both.",
        )

    handle_ids: list[int] | None = None
    chat_row_id: int | None = None
    chat_display_name: str | None = None
    chat_is_group = False

    if chat_id:
        chat_id = str(chat_id).strip()
        if not chat_id:
            return None, None, None, False, "Error: chat_id cannot be empty."
        chat = _chats._find_chat_by_identifier(chat_id)
        if not chat:
            return (
                None,
                None,
                None,
                False,
                (
                    f"No group chat found with chat_id '{chat_id}'. "
                    "Use tool_get_chats to list available group chats."
                ),
            )
        chat_row_id = chat["ROWID"]
        chat_display_name = chat.get("display_name") or chat_id
        # Only group chats (style 43) get the "[Name]" prefix; 1:1 and
        # business chats (style 45) would misattribute the thread.
        chat_is_group = chat.get("style") == 43

    # If contact is specified, try to resolve it
    if contact:
        # Convert to string to ensure phone numbers work properly
        contact = str(contact).strip()

        # Handle contact selection format (contact:N)
        if contact.lower().startswith("contact:"):
            # Extract the number after the colon
            contact_parts = contact.split(":", 1)
            if len(contact_parts) < 2 or not contact_parts[1].strip():
                return (
                    None,
                    None,
                    None,
                    False,
                    (
                        "Error: Invalid contact selection format. Use 'contact:N' "
                        "where N is a positive number."
                    ),
                )

            # Get the selected index (1-based)
            try:
                index = int(contact_parts[1].strip()) - 1
            except ValueError:
                return (
                    None,
                    None,
                    None,
                    False,
                    (
                        "Error: Contact selection must be a number. Use 'contact:N' "
                        "where N is a positive number."
                    ),
                )

            selection_error = _check_selection_index(index)
            if selection_error is not None:
                return None, None, None, False, selection_error

            recent_matches = _contacts.get_recent_contact_matches()
            if not recent_matches:
                return (
                    None,
                    None,
                    None,
                    False,
                    (
                        "No recent contact matches available. Please search for a "
                        "contact first."
                    ),
                )

            if index >= len(recent_matches):
                return (
                    None,
                    None,
                    None,
                    False,
                    (
                        "Invalid selection. Please choose a number between 1 and "
                        f"{len(recent_matches)}."
                    ),
                )

            # Get the selected contact's phone number
            contact = str(recent_matches[index]["phone"])

        # Check if contact might be a name rather than a phone number or email
        # If any character is NOT a phone/email character, treat as a name.
        # An address is named explicitly: it carries letters, so it would
        # otherwise be sent to fuzzy name matching, which returns "No contacts
        # found" for anyone whose address is not in the address book and never
        # reaches the handle lookup below.
        if not is_email_handle(contact) and not all(
            c.isdigit() or c in "+- ()@." for c in contact
        ):
            # Try fuzzy matching
            matches = _contacts.find_contact_by_name(contact)

            # An exact chat display-name match outranks weak AddressBook noise:
            # genuine name hits score 0.80+ (exact token 0.95, prefix 0.80+),
            # so AddressBook hits all near the 0.60 floor alongside an exact
            # chat match (e.g. "Poke" vs two unrelated humans) mean the chat.
            chat_match = None
            if not matches or _contacts._all_weak_matches(matches):
                chat_match = _chats._find_chat_by_display_name(contact)

            if chat_match is not None and not isinstance(chat_match, list):
                chat_row_id = chat_match["ROWID"]
                chat_display_name = chat_match.get("display_name") or contact
                chat_is_group = chat_match.get("style") == 43
            elif chat_match is not None and not matches:
                chat_list = "\n".join(
                    [
                        f"{i + 1}. {c['display_name']} "
                        f"(chat ID: {c['chat_identifier']})"
                        for i, c in enumerate(chat_match)
                    ],
                )
                return (
                    None,
                    None,
                    None,
                    False,
                    (
                        f"Multiple chats found matching '{contact}'. Please specify "
                        f"which one using 'chat_id' from tool_get_chats:\n{chat_list}"
                    ),
                )
            elif not matches:
                return (
                    None,
                    None,
                    None,
                    False,
                    f"No contacts found matching '{contact}'.",
                )
            elif len(matches) == 1:
                # Single match, use its phone number
                contact = str(matches[0]["phone"])
            elif _contacts._single_person(matches):
                # One person, several handles (phone + iMessage emails): read
                # across all of them rather than asking which to pick.
                handle_ids = []
                for m in matches:
                    handle_ids.extend(_contacts._handles_for_address(str(m["phone"])))
                handle_ids = list(dict.fromkeys(handle_ids))
                contact = str(_contacts._preferred_handle(matches)["phone"])
            else:
                # Store the matches for later selection (shared contact:N store)
                _contacts.set_recent_contact_matches(matches)

                # Multiple matches, return them all
                contact_list = "\n".join(
                    [
                        f"{i + 1}. {c['name']} ({c['phone']})"
                        for i, c in enumerate(matches[:10])
                    ],
                )
                return (
                    None,
                    None,
                    None,
                    False,
                    (
                        f"Multiple contacts found matching '{contact}'. Please "
                        "specify which one using 'contact:N' where N is the "
                        f"number:\n{contact_list}"
                    ),
                )

        # At this point, contact should be a phone number or email, unless the
        # chat display-name fallback above already resolved it (chat_row_id set)
        # or a single-person match already collected every handle.
        if chat_row_id is None:
            if not handle_ids:
                handle_ids = _contacts._handles_for_address(contact)

            if not handle_ids:
                # Try a direct search in message table to see if any messages exist.
                # Match on the digits of the canonical number so that a national
                # input still finds a handle stored in international format.
                normalized = digits_only(canonical_handle(contact) or contact)
                query = """
                SELECT COUNT(*) as count
                FROM message m
                JOIN handle h ON m.handle_id = h.ROWID
                WHERE h.id LIKE ?
                """
                results = _db.query_messages_db(query, (f"%{normalized}%",))

                if (
                    results
                    and "error" not in results[0]
                    and results[0].get("count", 0) == 0
                ):
                    # No messages found but the query was valid
                    return (
                        None,
                        None,
                        None,
                        False,
                        f"No message history found with '{contact}'.",
                    )
                # Could not find the handle at all
                return (
                    None,
                    None,
                    None,
                    False,
                    (
                        f"Could not find any messages with contact '{contact}'. "
                        "Verify the phone number or email is correct."
                    ),
                )

    return handle_ids, chat_row_id, chat_display_name, chat_is_group, None


@bound_untrusted_output
def get_recent_messages(
    hours: int = 24,
    contact: str | None = None,
    chat_id: str | None = None,
    *,
    limit: int = 100,
    offset: int = 0,
    start_date: str | None = None,
    end_date: str | None = None,
    unread_only: bool = False,
    since_rowid: int | None = None,
    format: str = "text",
) -> str:
    """Get recent messages from the Messages app using attributedBody for content.

    Args:
        hours: Number of hours to look back (default: 24). Ignored when
            ``start_date`` or ``end_date`` is given.
        contact: Filter by contact name, phone number, or email (optional)
                Use "contact:N" to select a specific contact from previous matches
        chat_id: Filter by group chat identifier from tool_get_chats (optional)
        limit: Maximum number of messages to return (default: 100, max 1000).
        offset: Number of newest messages to skip, for paging older history.
        start_date: Inclusive ISO date "YYYY-MM-DD" (UTC). Optional.
        end_date: Inclusive ISO date "YYYY-MM-DD" (UTC). Optional.
        unread_only: Return only inbound messages still marked unread.
        since_rowid: Return only messages newer than this message ROWID, in
            ascending order — the cursor form used for incremental reads.
        format: ``"text"`` (default) for one line per message, or ``"json"``
            for ``{"messages": [...], "cursor": {...}, "page": {...}}`` with
            one field per fact and ISO-8601 timestamps carrying the local
            offset.

    Returns:
        Formatted string with recent messages. Every text line carries the
        message ROWID as ``#N`` after the timestamp; a cursor read also ends
        with the highest ROWID seen so the caller knows what to pass next.

    """
    # Input validation
    format_error = _output._check_format(format)
    if format_error is not None:
        return format_error
    if hours < 0:
        return "Error: Hours cannot be negative. Please provide a positive number."

    # Prevent integer overflow - limit to reasonable maximum (10 years)
    max_hours = 10 * 365 * 24  # 87,600 hours
    if hours > max_hours:
        return (
            "Error: Hours value too large. Maximum allowed is "
            f"{max_hours} hours (10 years)."
        )
    if limit <= 0:
        return "Error: limit must be positive."
    if limit > _MAX_MESSAGE_LIMIT:
        return f"Error: limit must be at most {_MAX_MESSAGE_LIMIT}."
    if offset < 0:
        return "Error: offset cannot be negative."
    if since_rowid is not None and since_rowid < 0:
        return "Error: since_rowid cannot be negative."
    handle_ids, chat_row_id, chat_display_name, chat_is_group, scope_error = (
        _resolve_message_scope(contact, chat_id)
    )
    if scope_error is not None:
        return scope_error
    as_json = format == "json"

    # Time window: explicit dates win over the rolling `hours` window.
    window_clauses, window_params, date_error = _db.date_range_clauses(
        start_date,
        end_date,
    )
    if date_error is not None:
        return date_error
    if window_clauses:
        where_clauses = list(window_clauses)
        params: list[Any] = [*window_params]
    elif since_rowid is not None:
        # A cursor read means "everything after this row", independent of the
        # rolling hours window.
        where_clauses = []
        params = []
    else:
        # Calculate the timestamp for X hours ago.
        # String-bind the Apple-ns timestamp to avoid SQLite integer overflow.
        hours_ago = datetime.now(timezone.utc) - timedelta(hours=hours)
        where_clauses = ["CAST(m.date AS TEXT) > ?"]
        params = [str(_db.to_apple_ns(hours_ago))]

    # Add contact filter if handle_ids were found (support multiple handles
    # for multi-protocol)
    if handle_ids:
        placeholders = ", ".join(["?" for _ in handle_ids])
        where_clauses.append(f"m.handle_id IN ({placeholders})")
        params.extend(handle_ids)

    if chat_row_id is not None:
        where_clauses.append(
            "m.ROWID IN (SELECT message_id FROM chat_message_join "
            "WHERE chat_id = ?)",
        )
        params.append(chat_row_id)

    if unread_only:
        where_clauses.append("m.is_from_me = 0 AND m.is_read = 0")

    if since_rowid is not None:
        where_clauses.append("m.ROWID > ?")
        params.append(since_rowid)

    # A cursor read streams forward in time; the default read shows the newest
    # messages first.
    order = "ASC" if since_rowid is not None else "DESC"
    where_sql = " AND ".join(where_clauses)

    # Build the SQL query - use attributedBody field and text. The metadata
    # columns (service, read/delivery flags, tapback, reply) are rendered only
    # when the row carries them, so an older chat.db schema degrades to the
    # original line format rather than failing.
    query = f"""
    SELECT
        {_message_columns()}
    FROM
        message m
    WHERE
        {where_sql}
    ORDER BY m.date {order}
    LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])

    # Execute the query
    messages = _db.query_messages_db(query, tuple(params))

    def _json_page(records: list[dict[str, Any]], newest: int | None) -> str:
        next_cursor = newest if newest is not None else since_rowid
        return _output._json_dumps(
            {
                "messages": [_message_json(r) for r in records],
                "cursor": {"next_since_rowid": next_cursor},
                "page": {
                    "limit": limit,
                    "offset": offset,
                    "has_more": len(messages) >= limit,
                },
            }
        )

    # Format the results
    if not messages:
        if as_json:
            return _json_page([], None)
        if since_rowid is not None:
            return f"No new messages since ROWID {since_rowid}."
        return "No messages found in the specified time period."

    if "error" in messages[0]:
        if as_json:
            return _output._json_dumps(
                {"error": _output._json_text(messages[0]["error"])}
            )
        return f"Error accessing messages: {messages[0]['error']}"

    # Get chat mapping for group chat names
    chat_mapping = _chats.get_chat_mapping()

    # Bulk-fetch attachments for all message ROWIDs in one query (Tier 1
    # progressive disclosure).
    visible_ids: list[int] = [
        msg["ROWID"] for msg in messages if msg.get("ROWID") is not None
    ]
    attachments_by_msg = _attachments._attachments_for_message_ids(visible_ids)

    records = _build_message_records(
        messages,
        chat_mapping=chat_mapping,
        attachments_by_msg=attachments_by_msg,
        chat_display_name=chat_display_name,
        chat_is_group=chat_is_group,
    )

    # A cursor read must always hand back the next cursor, even when every
    # fetched row rendered nothing (attachment-only rows, deleted bodies):
    # those rows still advance the cursor, and reporting "no new messages"
    # would make a polling caller re-read them forever.
    newest_rowid = _newest_rowid(messages)

    if as_json:
        return _json_page(records, newest_rowid)

    formatted_messages = [_render_message_line(r) for r in records]

    if not formatted_messages:
        if since_rowid is not None:
            if newest_rowid is not None and newest_rowid > since_rowid:
                return (
                    f"No displayable new messages since ROWID {since_rowid}; "
                    f"{_cursor_note(newest_rowid)}"
                )
            return f"No new messages since ROWID {since_rowid}."
        return "No messages found in the specified time period."

    if since_rowid is not None and newest_rowid is not None:
        note = _cursor_note(newest_rowid)
        if len(messages) >= limit:
            note = "More may exist; " + note
        formatted_messages.append(f"({note})")
    elif len(messages) >= limit:
        formatted_messages.append(
            f"(Showing {len(messages)} message(s) from offset {offset}; older "
            f"messages may exist - call again with offset={offset + limit}.)",
        )

    return "\n".join(formatted_messages)


def _message_line_prefix(date_str: str, rowid: Any) -> str:
    """``[date] #rowid`` — the ROWID is the cursor ``since_rowid`` consumes."""
    prefix = f"[{date_str}]"
    if rowid is not None:
        prefix += f" #{rowid}"
    return prefix


def _newest_rowid(rows: list[dict[str, Any]]) -> int | None:
    """Highest message ROWID among fetched rows, or None when there is none."""
    rowids: list[int] = []
    for row in rows:
        try:
            rowids.append(int(row["ROWID"]))
        except (KeyError, TypeError, ValueError):
            continue
    return max(rowids) if rowids else None


def _cursor_note(newest_rowid: int) -> str:
    """Tell a polling caller exactly what to pass next."""
    return (
        f"cursor: highest ROWID seen is {newest_rowid}; pass "
        f"since_rowid={newest_rowid} for anything newer."
    )


# Upper bound for one tool_wait_for_new_messages call. The MCP stdio transport
# has no server push, so waiting is a bounded poll; a client that needs a longer
# horizon calls this tool repeatedly.
_MAX_WAIT_SECONDS = 300


_WAIT_BATCH_LIMIT = 50


@bound_untrusted_output
def wait_for_new_messages(
    since_rowid: int = 0,
    timeout_seconds: float = 30.0,
    poll_interval: float = 1.0,
    contact: str | None = None,
    chat_id: str | None = None,
    format: str = "text",
) -> str:
    """Block until a message newer than ``since_rowid`` exists, or time out.

    An MCP stdio server cannot push, so this is a bounded poll: it returns the
    new messages as soon as any appear *within the given scope*, or a timeout
    notice. Call it in a loop, passing the highest ROWID returned last time
    (the ``#N`` on each line, repeated in the closing cursor note) as the next
    ``since_rowid``. A ``contact`` or ``chat_id`` filter narrows what wakes
    the wait, not just what is rendered.

    Args:
        since_rowid: Cursor; only messages with a greater ROWID are returned.
        timeout_seconds: How long to wait before giving up (max 300).
        poll_interval: Seconds between database polls.
        contact: Optional contact filter, as in ``get_recent_messages``.
        chat_id: Optional conversation filter, as in ``get_recent_messages``.
        format: ``"text"`` or ``"json"``; a JSON timeout is
            ``{"messages": [], "cursor": {...}, "timed_out": true}``.

    """
    format_error = _output._check_format(format)
    if format_error is not None:
        return format_error
    if since_rowid < 0:
        return "Error: since_rowid cannot be negative."
    if timeout_seconds <= 0 or timeout_seconds > _MAX_WAIT_SECONDS:
        return f"Error: timeout_seconds must be between 0 and {_MAX_WAIT_SECONDS}."
    if poll_interval <= 0:
        return "Error: poll_interval must be positive."

    # Resolve the contact/chat filter once, up front, so an unresolvable
    # scope is reported now instead of after the timeout, and so the probe
    # below watches only this scope. Probing the whole table and then
    # filtering the read used to wake the wait for any thread's traffic and
    # return "no new messages" without advancing the cursor, which left a
    # polling caller spinning.
    handle_ids, chat_row_id, _chat_name, _chat_is_group, scope_error = (
        _resolve_message_scope(contact, chat_id)
    )
    if scope_error is not None:
        return scope_error

    probe_clauses = ["m.ROWID > ?"]
    probe_params: list[Any] = [since_rowid]
    if handle_ids:
        placeholders = ", ".join(["?" for _ in handle_ids])
        probe_clauses.append(f"m.handle_id IN ({placeholders})")
        probe_params.extend(handle_ids)
    if chat_row_id is not None:
        probe_clauses.append(
            "m.ROWID IN (SELECT message_id FROM chat_message_join "
            "WHERE chat_id = ?)",
        )
        probe_params.append(chat_row_id)
    probe_query = (
        "SELECT COALESCE(MAX(m.ROWID), 0) AS max_rowid FROM message m "
        f"WHERE {' AND '.join(probe_clauses)}"
    )

    deadline = time.monotonic() + timeout_seconds
    while True:
        rows = _db.query_messages_db(probe_query, tuple(probe_params))
        max_rowid = 0
        if rows and "error" not in rows[0]:
            try:
                max_rowid = int(rows[0].get("max_rowid") or 0)
            except (TypeError, ValueError):
                max_rowid = 0
        if max_rowid > since_rowid:
            kwargs: dict[str, Any] = {
                "since_rowid": since_rowid,
                "contact": contact,
                "chat_id": chat_id,
                "limit": _WAIT_BATCH_LIMIT,
            }
            if format != "text":
                kwargs["format"] = format
            return get_recent_messages(**kwargs)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if format == "json":
                return _output._json_dumps(
                    {
                        "messages": [],
                        "cursor": {"next_since_rowid": since_rowid},
                        "timed_out": True,
                        "waited_seconds": timeout_seconds,
                    }
                )
            return (
                f"No new messages arrived within {timeout_seconds:g} seconds "
                f"(cursor ROWID {since_rowid})."
            )
        time.sleep(min(poll_interval, remaining))


# Maximum number of messages returned by a single fuzzy search query.
# A soft cap — if hit, the user is told results were truncated.
_FUZZY_SEARCH_SOFT_CAP = 10_000


@bound_untrusted_output
def fuzzy_search_messages(
    search_term: str,
    hours: int = 720,
    threshold: float = 0.6,  # Default threshold adjusted for thefuzz
    *,
    contact: str | None = None,
    chat_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 100,
    format: str = "text",
) -> str:
    """Fuzzy search for messages containing the search_term within the last N hours.

    Args:
        search_term: The string to search for in message content.
        hours: Number of hours to look back (default: 720, i.e. 30 days).
               Use 0 to search all messages with no time limit. Ignored when
               ``start_date`` or ``end_date`` is given.
        threshold: Minimum similarity score (0.0-1.0) to consider a match
            (default: 0.6 for WRatio). A lower threshold allows for more
            lenient matching.
        contact: Restrict the search to one contact, phone number, email, or
            "contact:N" selector.
        chat_id: Restrict the search to one conversation from tool_get_chats.
        start_date: Inclusive ISO date "YYYY-MM-DD" (UTC). Optional.
        end_date: Inclusive ISO date "YYYY-MM-DD" (UTC). Optional.
        limit: Maximum number of ranked matches to return (default: 100).
        format: ``"text"`` or ``"json"`` (``{"query", "messages", ...}``; each
            message carries its ``score``).

    Returns:
        Formatted string with matching messages and scores, or an error
        message when there are no results.

    """
    # Input validation
    format_error = _output._check_format(format)
    if format_error is not None:
        return format_error
    as_json = format == "json"
    if not search_term or not search_term.strip():
        return "Error: Search term cannot be empty."

    if hours < 0:
        return "Error: Hours cannot be negative. Please provide a positive number."

    # Prevent integer overflow - limit to reasonable maximum (10 years)
    max_hours = 10 * 365 * 24  # 87,600 hours
    if hours > max_hours:
        return (
            "Error: Hours value too large. Maximum allowed is "
            f"{max_hours} hours (10 years)."
        )
    if not 0.0 <= threshold <= 1.0:
        return "Error: Threshold must be between 0.0 and 1.0."
    if limit <= 0:
        return "Error: limit must be positive."

    handle_ids, chat_row_id, chat_display_name, chat_is_group, scope_error = (
        _resolve_message_scope(contact, chat_id)
    )
    if scope_error is not None:
        return scope_error

    # Build the SQL query — use a LIKE pre-filter on the text column to let
    # SQLite do the heavy lifting for exact substring matches.  Messages
    # stored only in attributedBody (binary blob) cannot be LIKE-searched,
    # so we also fetch those and filter in Python.
    escaped_term = _db._escape_like(search_term)
    like_param = f"%{escaped_term}%"

    like_clause = (
        "(m.text LIKE ? ESCAPE '\\' OR "
        "(m.text IS NULL AND m.attributedBody IS NOT NULL))"
    )
    # Time window: explicit dates replace the rolling `hours` window.
    time_clauses, time_params, date_error = _db.date_range_clauses(
        start_date,
        end_date,
    )
    if date_error is not None:
        return date_error
    if time_clauses:
        time_desc = f"{start_date or 'the beginning'} to {end_date or 'now'}"
    elif hours == 0:
        time_desc = "all time"
    else:
        hours_ago_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
        # String-bind the Apple-ns timestamp to avoid SQLite integer overflow.
        time_clauses = ["CAST(m.date AS TEXT) > ?"]
        time_params = [str(_db.to_apple_ns(hours_ago_dt))]
        time_desc = f"the last {hours} hours"

    # Ranking happens in Python, so fetch a multiple of the requested page and
    # keep the existing soft cap as the ceiling on one query's cost.
    fetch_limit = min(_FUZZY_SEARCH_SOFT_CAP, max(limit * 10, 500))

    # Prefer the full-history index: it names the rows that mention every
    # word of the term across all time, so the bounded scan below is only a
    # fallback for when the index is disabled or cannot answer.
    index_rowids = _search_index.candidate_rowids(search_term, fetch_limit)
    if index_rowids is not None:
        if not index_rowids:
            if as_json:
                return _json_empty()
            return (
                f"No messages found matching '{search_term}' with a threshold "
                f"of {threshold} in {time_desc}."
            )
        placeholders = ", ".join("?" for _ in index_rowids)
        where_clauses = [*time_clauses, f"m.ROWID IN ({placeholders})"]
        params_list: list[Any] = [*time_params, *index_rowids]
    else:
        where_clauses = [*time_clauses, like_clause]
        params_list = [*time_params, like_param]

    if handle_ids:
        placeholders = ", ".join(["?" for _ in handle_ids])
        where_clauses.append(f"m.handle_id IN ({placeholders})")
        params_list.extend(handle_ids)

    if chat_row_id is not None:
        where_clauses.append(
            "m.ROWID IN (SELECT message_id FROM chat_message_join "
            "WHERE chat_id = ?)",
        )
        params_list.append(chat_row_id)

    params_list.append(fetch_limit)
    where_sql = " AND ".join(where_clauses)
    query = f"""
    SELECT
        {_message_columns()}
    FROM
        message m
    WHERE
        {where_sql}
    ORDER BY m.date DESC
    LIMIT ?
    """
    params = tuple(params_list)

    raw_messages = _db.query_messages_db(query, params)

    def _json_empty() -> str:
        return _output._json_dumps(
            {
                "query": _output._json_text(search_term),
                "threshold": threshold,
                "messages": [],
                "candidates_truncated": False,
                "results_truncated": False,
            }
        )

    if not raw_messages:
        return (
            _json_empty() if as_json else f"No messages found in {time_desc} to search."
        )
    if "error" in raw_messages[0]:
        if as_json:
            return _output._json_dumps(
                {"error": _output._json_text(raw_messages[0]["error"])}
            )
        return f"Error accessing messages: {raw_messages[0]['error']}"

    message_candidates: list[tuple[str, dict[str, Any]]] = []
    for msg_dict in raw_messages:
        body = msg_dict.get("text") or extract_body_from_attributed(
            msg_dict.get("attributedBody"),
        )
        if body and not _is_attachment_placeholder_body(body):
            body = _strip_attachment_placeholders(body)
        if body and body.strip():
            message_candidates.append((body, msg_dict))

    if not message_candidates:
        if as_json:
            return _json_empty()
        return f"No message content found to search in {time_desc}."

    # --- Two-pass matching: exact substring first, then fuzzy ---
    cleaned_search_term = _contacts._clean_text(search_term).lower()
    # thefuzz scores are 0-100. Scale the input threshold (0.0-1.0).
    scaled_threshold = threshold * 100

    matched_messages_with_scores: list[tuple[str, dict[str, Any], float]] = []
    for original_message_text, msg_dict_value in message_candidates:
        cleaned_candidate_text = _contacts._clean_text(original_message_text).lower()

        # Pass 1: exact substring match gets a perfect score
        if cleaned_search_term in cleaned_candidate_text:
            score_normalised = 1.0
        else:
            # Pass 2: fuzzy match via WRatio
            score_from_thefuzz = fuzz.WRatio(
                cleaned_search_term,
                cleaned_candidate_text,
            )
            if score_from_thefuzz < scaled_threshold:
                continue
            score_normalised = score_from_thefuzz / 100.0

        matched_messages_with_scores.append(
            (original_message_text, msg_dict_value, score_normalised),
        )

    matched_messages_with_scores.sort(
        key=operator.itemgetter(2),
        reverse=True,
    )  # Sort by score desc

    if not matched_messages_with_scores:
        if as_json:
            return _json_empty()
        return (
            f"No messages found matching '{search_term}' with a threshold "
            f"of {threshold} in {time_desc}."
        )

    fetch_truncated = index_rowids is None and len(raw_messages) >= fetch_limit
    results_truncated = len(matched_messages_with_scores) > limit
    matched_messages_with_scores = matched_messages_with_scores[:limit]

    chat_mapping = _chats.get_chat_mapping()

    # Bulk-fetch attachments for all matched message ROWIDs in one query
    # (Tier 1 progressive disclosure).
    matched_rows = [m[1] for m in matched_messages_with_scores]
    scores = {
        m[1]["ROWID"]: m[2]
        for m in matched_messages_with_scores
        if m[1].get("ROWID") is not None
    }
    matched_ids: list[int] = list(scores)
    attachments_by_msg = _attachments._attachments_for_message_ids(matched_ids)
    records = _build_message_records(
        matched_rows,
        chat_mapping=chat_mapping,
        attachments_by_msg=attachments_by_msg,
        scores=scores,
    )
    # A match whose row has no displayable content still matched on something;
    # keep the ranking order from matched_messages_with_scores.
    formatted_results = [_render_message_line(r) for r in records]

    if as_json:
        return _output._json_dumps(
            {
                "query": _output._json_text(search_term),
                "threshold": threshold,
                "messages": [_message_json(r) for r in records],
                "candidates_truncated": fetch_truncated,
                "results_truncated": results_truncated,
            }
        )

    header = (
        f"Found {len(matched_messages_with_scores)} messages "
        f"matching '{search_term}':\n"
    )
    if fetch_truncated:
        header += (
            f"(Search stopped after {fetch_limit} candidate messages — try a "
            "shorter time window for more precise results.)\n"
        )
    if results_truncated:
        header += (
            f"(Showing the {limit} best matches; raise 'limit' or narrow the "
            "time window for more.)\n"
        )
    return header + "\n".join(formatted_results)
