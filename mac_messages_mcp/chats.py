# Copyright (c) 2023 Carter Lasalle
"""Chats: lookup by identifier or name, group membership, conversation listing,
and the group-chat labels used on message lines."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from . import contacts as _contacts
from . import db as _db
from . import output as _output
from .output import _MAX_MESSAGE_LIMIT
from .untrusted import bound_untrusted_output, neutralize_untrusted_text

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)


# Chat-table caches, keyed by database path and a (count, max ROWID)
# fingerprint of the chat table. Chats are created rarely and renamed rarely,
# and the fingerprint query is an index-only lookup, so every page of messages
# no longer re-reads the chat table or re-resolves the same chat identifier.
_CHAT_FINGERPRINT_QUERY = (
    "SELECT COUNT(*) AS n, COALESCE(MAX(ROWID), 0) AS max_rowid FROM chat"
)
_CHAT_MAPPING_CACHE: dict[str, tuple[tuple[Any, Any], dict[str, str]]] = {}
_CHAT_LOOKUP_CACHE: dict[
    str, tuple[tuple[Any, Any], dict[str, dict[str, Any] | None]]
] = {}


def _chat_fingerprint() -> tuple[Any, Any] | None:
    """(row count, max ROWID) of the chat table, or None when unreadable
    (including a mocked query that returns something else)."""
    rows = _db.query_messages_db(_CHAT_FINGERPRINT_QUERY)
    if not rows or "error" in rows[0] or "n" not in rows[0]:
        return None
    return (rows[0].get("n"), rows[0].get("max_rowid"))


def clear_chat_caches() -> None:
    _CHAT_MAPPING_CACHE.clear()
    _CHAT_LOOKUP_CACHE.clear()


def get_chat_mapping() -> dict[str, str]:
    """Get mapping from room_name to display_name for group chats (style 43).

    1:1 and business chats (style 45) carry a display_name too, but prefixing
    their messages with it misattributes the thread (e.g. "[Poke] You:").
    Only group chats get the "[Name]" prefix.

    Returns an empty dict if the database is inaccessible or locked. Cached
    per database until the chat table's fingerprint changes.
    """
    db_path = _db.get_messages_db_path()
    fingerprint = _chat_fingerprint()
    if fingerprint is not None:
        cached = _CHAT_MAPPING_CACHE.get(db_path)
        if cached is not None and cached[0] == fingerprint:
            return dict(cached[1])
    mapping = _load_chat_mapping()
    if fingerprint is not None:
        _CHAT_MAPPING_CACHE[db_path] = (fingerprint, mapping)
    return dict(mapping)


def _load_chat_mapping() -> dict[str, str]:
    rows = _db.query_messages_db(
        "SELECT room_name, display_name FROM chat WHERE style = 43"
    )
    if rows and "error" in rows[0] and "style" in str(rows[0]["error"]):
        # Schema without style (older DBs / minimal test fixtures).
        rows = _db.query_messages_db("SELECT room_name, display_name FROM chat")
    if not rows or "error" in rows[0]:
        if rows:
            logger.warning("Error reading chat mapping: %s", rows[0]["error"])
        return {}
    return {
        row["room_name"]: row["display_name"]
        for row in rows
        if "room_name" in row and "display_name" in row
    }


_GROUP_LABEL_NAMES = 3


def _chats_for_message_ids(message_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Map message ROWID -> its chat row (ROWID, identifier, name, style).

    Rows that do not look like chat-join rows are ignored, so a mocked query
    serving several statements cannot poison the map.
    """
    if not message_ids:
        return {}
    placeholders = ", ".join("?" for _ in message_ids)
    rows = _db.query_messages_db(
        "SELECT cmj.message_id AS message_id, c.ROWID AS chat_row_id, "
        "c.chat_identifier AS chat_identifier, c.display_name AS display_name, "
        "c.style AS style "
        "FROM chat_message_join cmj JOIN chat c ON c.ROWID = cmj.chat_id "
        f"WHERE cmj.message_id IN ({placeholders})",
        tuple(message_ids),
    )
    if not rows or "error" in rows[0]:
        return {}
    chats: dict[int, dict[str, Any]] = {}
    for row in rows:
        if "message_id" in row and "chat_row_id" in row and "chat_identifier" in row:
            chats.setdefault(row["message_id"], row)
    return chats


def _participants_for_chats(chat_row_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Map chat ROWID -> [{handle_id, address, name}], names resolved in a batch."""
    if not chat_row_ids:
        return {}
    placeholders = ", ".join("?" for _ in chat_row_ids)
    rows = _db.query_messages_db(
        "SELECT chj.chat_id AS chat_id, h.ROWID AS handle_id, h.id AS address "
        "FROM chat_handle_join chj JOIN handle h ON h.ROWID = chj.handle_id "
        f"WHERE chj.chat_id IN ({placeholders}) ORDER BY chj.chat_id, h.ROWID",
        tuple(chat_row_ids),
    )
    if not rows or "error" in rows[0]:
        return {}
    rows = [r for r in rows if "chat_id" in r and "handle_id" in r and "address" in r]
    _contacts.prime_contact_names(r["handle_id"] for r in rows)
    participants: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        participants.setdefault(row["chat_id"], []).append(
            {
                "handle_id": row["handle_id"],
                "address": row["address"],
                "name": _contacts.get_contact_name(row["handle_id"]),
            }
        )
    return participants


def _group_label(participants: list[dict[str, Any]]) -> str:
    """``group: A, B, C +N`` for a group chat with no display name."""
    names = [neutralize_untrusted_text(p["name"]) for p in participants]
    shown = names[:_GROUP_LABEL_NAMES]
    extra = len(names) - len(shown)
    label = ", ".join(shown) if shown else "unknown participants"
    if extra > 0:
        label += f" +{extra}"
    return f"group: {label}"


def _chat_json(
    chat: dict[str, Any] | None, participants: list[dict[str, Any]] | None
) -> dict[str, Any] | None:
    if chat is None:
        return None
    kind = _conversation_kind(chat.get("style"), chat.get("chat_identifier"))
    item: dict[str, Any] = {
        "id": _output._json_text(chat.get("chat_identifier")) or None,
        "name": _output._json_text(chat.get("display_name")) or None,
        "kind": kind,
    }
    if participants is not None:
        item["participants"] = [_output._participant_json(p) for p in participants]
    return item


def _find_chat_by_identifier(chat_id: str) -> dict[str, Any] | None:
    """Find a Messages chat row by chat_identifier or room_name.

    Results (including misses) are cached per database until the chat table's
    fingerprint changes, so a scoped poll does not re-resolve its chat every
    second.
    """
    chat_id = str(chat_id).strip()
    if not chat_id:
        return None
    db_path = _db.get_messages_db_path()
    fingerprint = _chat_fingerprint()
    if fingerprint is not None:
        cached = _CHAT_LOOKUP_CACHE.get(db_path)
        if cached is not None and cached[0] == fingerprint and chat_id in cached[1]:
            hit = cached[1][chat_id]
            return dict(hit) if hit is not None else None
    row = _lookup_chat_by_identifier(chat_id)
    if fingerprint is not None:
        cached = _CHAT_LOOKUP_CACHE.get(db_path)
        if cached is None or cached[0] != fingerprint:
            cached = (fingerprint, {})
            _CHAT_LOOKUP_CACHE[db_path] = cached
        # Every spelling of the resolved chat points at the same row, so a
        # later lookup by the other form is a hit too.
        keys = {chat_id}
        if row is not None:
            for value in (row.get("chat_identifier"), row.get("room_name")):
                if value:
                    keys |= _identifier_spellings(str(value))
        for key in keys:
            cached[1][key] = dict(row) if row is not None else None
    return row


def _identifier_spellings(chat_id: str) -> set[str]:
    """The forms a chat identifier may be written in."""
    variants = {chat_id}
    if chat_id.startswith("chat"):
        variants.add(f"iMessage;-;{chat_id}")
        variants.add(f"iMessage;+;{chat_id}")
    elif chat_id.startswith("iMessage;"):
        short_id = chat_id.rsplit(";", 1)[-1]
        if short_id.startswith("chat"):
            variants.add(short_id)
    return variants


def _lookup_chat_by_identifier(chat_id: str) -> dict[str, Any] | None:
    variants = _identifier_spellings(chat_id)

    placeholders = ", ".join(["?" for _ in variants])
    query = f"""
    SELECT ROWID, display_name, chat_identifier, room_name, style
    FROM chat
    WHERE chat_identifier IN ({placeholders})
       OR room_name IN ({placeholders})
    LIMIT 1
    """
    params = tuple(variants) + tuple(variants)
    rows = _db.query_messages_db(query, params)
    if not rows or "error" in rows[0]:
        return None
    return rows[0]


def _find_chat_by_display_name(
    name: str,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Find Messages chat rows by display name (groups and 1:1/business chats).

    Returns the single matching row, a list when the name is ambiguous, or
    None when nothing matches. Exact case-insensitive match.
    """
    name = str(name).strip()
    if not name:
        return None
    rows = _db.query_messages_db(
        "SELECT ROWID, display_name, chat_identifier, style FROM chat "
        "WHERE display_name = ? COLLATE NOCASE LIMIT 2",
        (name,),
    )
    if not rows or "error" in rows[0]:
        return None
    if len(rows) == 1:
        return rows[0]
    return rows


def list_named_chats() -> list[dict[str, Any]]:
    """Return ``{chat_identifier, display_name}`` rows for chats carrying a name.

    Shared by ``tool_get_chats`` and the CLI ``chats`` command. A database
    failure comes back as ``query_messages_db``'s single ``{"error": ...}`` row.
    """
    rows = _db.query_messages_db(
        "SELECT ROWID AS chat_row_id, chat_identifier, display_name, style "
        "FROM chat WHERE display_name IS NOT NULL OR style = 43"
    )
    if rows and "error" in rows[0]:
        return rows
    kept = [
        dict(row) for row in rows if row.get("display_name") or row.get("style") == 43
    ]
    unnamed = [
        r["chat_row_id"]
        for r in kept
        if not r.get("display_name") and r.get("chat_row_id") is not None
    ]
    participants = _participants_for_chats(unnamed)
    for row in kept:
        if not row.get("display_name"):
            chat_row_id = row.get("chat_row_id")
            row["participants"] = (
                participants.get(chat_row_id, []) if chat_row_id is not None else []
            )
    return kept


def format_chat_label(row: dict[str, Any]) -> str:
    """``Family`` for a named chat, ``(unnamed: A, B, C)`` for a group without one."""
    name = row.get("display_name")
    if name:
        return neutralize_untrusted_text(name)
    names = [
        neutralize_untrusted_text(p["name"]) for p in row.get("participants") or []
    ]
    shown = names[:_GROUP_LABEL_NAMES]
    extra = len(names) - len(shown)
    label = ", ".join(shown) if shown else "unknown participants"
    if extra > 0:
        label += f" +{extra}"
    return f"(unnamed: {label})"


@bound_untrusted_output
def get_chat_participants(chat_id: str, format: str = "text") -> str:
    """List who is in a chat, by name and address.

    Accepts the same identifiers as the chat_id filter on reads. Works for
    1:1 threads too (one participant).
    """
    format_error = _output._check_format(format)
    if format_error is not None:
        return format_error
    chat = _find_chat_by_identifier(chat_id)
    if chat is None:
        return (
            f"No chat found with chat_id '{chat_id}'. Use tool_get_chats or "
            "tool_list_conversations to find one."
        )
    members = _participants_for_chats([chat["ROWID"]]).get(chat["ROWID"], [])
    if format == "json":
        return _output._json_dumps(
            {
                "chat": _chat_json(
                    {
                        "chat_identifier": chat.get("chat_identifier"),
                        "display_name": chat.get("display_name"),
                        "style": chat.get("style"),
                    },
                    None,
                ),
                "participants": [_output._participant_json(p) for p in members],
            }
        )
    label = chat.get("display_name") or chat.get("chat_identifier") or chat_id
    lines = [
        f"{neutralize_untrusted_text(label)} (chat ID: "
        f"{neutralize_untrusted_text(chat.get('chat_identifier'))}): "
        f"{len(members)} participant(s)"
    ]
    for p in members:
        lines.append(
            f"- {neutralize_untrusted_text(p['name'])} "
            f"({neutralize_untrusted_text(p['address'])})"
        )
    return "\n".join(lines)


# Chat styles macOS writes into chat.style: 43 is a group thread and 45 a
# one-to-one thread. Business (Apple Messages for Business) threads are also
# style 45; they are told apart by their "urn:biz:..." chat identifier.
_CONVERSATION_STYLES: dict[int, str] = {43: "group", 45: "direct"}


_BUSINESS_CHAT_PREFIX = "urn:biz"


def _conversation_kind(style: Any, chat_identifier: Any = None) -> str:
    """Label a chat as group, business, or direct.

    ``style`` alone cannot separate a business thread from an ordinary 1:1
    thread (both are 45); the business identifier prefix does.
    """
    if str(chat_identifier or "").startswith(_BUSINESS_CHAT_PREFIX):
        return "business"
    try:
        return _CONVERSATION_STYLES.get(int(style), "direct")
    except (TypeError, ValueError):
        return "direct"


# Per-chat activity comes from chat_message_join.message_date, which Apple
# maintains and indexes, so the message table is never touched for counts or
# last activity. Unread and unseen counts start from the small set of rows that
# qualify (the unread index; rows newer than the chat's last-read marker) rather
# than from every message. The previous shape joined chat -> join -> message for
# the whole table on every call. In the unseen CTE, CROSS JOIN pins the loop
# order: per chat, range-scan the (chat_id, message_date) index from the
# last-read marker, then look up only those few messages; left to itself the
# planner scanned the whole join table.
_CONVERSATIONS_QUERY = """
    WITH activity AS (
        SELECT chat_id,
               COUNT(*) AS message_count,
               MAX(message_date) AS last_message_date
        FROM chat_message_join
        GROUP BY chat_id
    ),
    unread AS (
        SELECT cmj.chat_id,
               COUNT(*) AS unread_count,
               SUM(CASE WHEN m.has_unseen_mention = 1 THEN 1 ELSE 0 END)
                   AS unseen_mentions
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        WHERE m.is_read = 0 AND m.is_from_me = 0
        GROUP BY cmj.chat_id
    ),
    unseen AS (
        SELECT cmj.chat_id, COUNT(*) AS unseen_count
        FROM chat c
        CROSS JOIN chat_message_join cmj
          ON cmj.chat_id = c.ROWID
         AND cmj.message_date > c.last_read_message_timestamp
        CROSS JOIN message m ON m.ROWID = cmj.message_id
        WHERE c.last_read_message_timestamp > 0 AND m.is_from_me = 0
        GROUP BY cmj.chat_id
    )
    SELECT
        c.ROWID AS chat_row_id,
        c.chat_identifier AS chat_identifier,
        c.display_name AS display_name,
        c.style AS style,
        c.is_archived AS is_archived,
        c.is_filtered AS is_filtered,
        c.last_read_message_timestamp AS last_read_message_timestamp,
        COALESCE(a.message_count, 0) AS message_count,
        COALESCE(u.unread_count, 0) AS unread_count,
        COALESCE(u.unseen_mentions, 0) AS unseen_mentions,
        COALESCE(s.unseen_count, 0) AS unseen_count,
        a.last_message_date AS last_message_date
    FROM chat c
    LEFT JOIN activity a ON a.chat_id = c.ROWID
    LEFT JOIN unread u ON u.chat_id = c.ROWID
    LEFT JOIN unseen s ON s.chat_id = c.ROWID
    WHERE (? OR COALESCE(c.is_archived, 0) = 0)
    ORDER BY a.last_message_date IS NULL, a.last_message_date DESC
    LIMIT ?
"""

# For a chat.db predating is_archived / is_filtered / last_read_message_timestamp.
_CONVERSATIONS_QUERY_LEGACY = """
    SELECT
        c.ROWID AS chat_row_id,
        c.chat_identifier AS chat_identifier,
        c.display_name AS display_name,
        c.style AS style,
        COUNT(m.ROWID) AS message_count,
        SUM(CASE WHEN m.is_from_me = 0 AND m.is_read = 0 THEN 1 ELSE 0 END)
            AS unread_count,
        MAX(m.date) AS last_message_date
    FROM chat c
    LEFT JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
    LEFT JOIN message m ON m.ROWID = cmj.message_id
    GROUP BY c.ROWID, c.chat_identifier, c.display_name, c.style
    ORDER BY last_message_date IS NULL, last_message_date DESC
    LIMIT ?
"""


def _conversation_rows(limit: int, include_archived: bool) -> list[dict[str, Any]]:
    rows = _db.query_messages_db(
        _CONVERSATIONS_QUERY, (1 if include_archived else 0, limit)
    )
    if rows and "error" in rows[0] and "no such column" in str(rows[0]["error"]):
        rows = _db.query_messages_db(_CONVERSATIONS_QUERY_LEGACY, (limit,))
    return rows


@bound_untrusted_output
def list_conversations(
    limit: int = 50,
    unread_only: bool = False,
    format: str = "text",
    include_archived: bool = False,
) -> str:
    """List every conversation, most recent activity first.

    Unlike ``get_chat_mapping`` (named group chats only), this covers 1:1,
    business, and group threads, with message and unread counts, so a client can
    discover a conversation and then page it with ``get_recent_messages`` or
    ``fuzzy_search_messages``. Archived threads are hidden unless asked for;
    threads Messages filed under Unknown Senders are tagged ``[unknown sender]``.
    When Messages has recorded when you last opened a thread, the count of
    inbound messages newer than that is reported as "since last opened", which
    is a steadier signal than the per-message read flag.

    Args:
        limit: Maximum number of conversations to return (default 50).
        unread_only: Return only conversations with at least one unread message.
        format: ``"text"`` or ``"json"`` (``{"conversations": [...]}``).
        include_archived: Also list archived threads, tagged ``[archived]``.

    """
    format_error = _output._check_format(format)
    if format_error is not None:
        return format_error
    if limit <= 0:
        return "Error: limit must be positive."
    if limit > _MAX_MESSAGE_LIMIT:
        return f"Error: limit must be at most {_MAX_MESSAGE_LIMIT}."
    as_json = format == "json"

    rows = _conversation_rows(limit, include_archived)
    if not rows:
        return (
            _output._json_dumps({"conversations": []})
            if as_json
            else "No conversations found."
        )
    if "error" in rows[0]:
        if as_json:
            return _output._json_dumps({"error": _output._json_text(rows[0]["error"])})
        return f"Error accessing chats: {rows[0]['error']}"

    lines: list[str] = []
    items: list[dict[str, Any]] = []
    for row in rows:
        unread = int(row.get("unread_count") or 0)
        unseen = int(row.get("unseen_count") or 0)
        if unread_only and not (unread or unseen):
            continue
        archived = bool(row.get("is_archived"))
        filtered = bool(row.get("is_filtered"))
        mentions = int(row.get("unseen_mentions") or 0)
        if archived and not include_archived:
            continue  # legacy query cannot filter in SQL
        name = row.get("display_name") or row.get("chat_identifier") or "unknown"
        last = row.get("last_message_date")
        last_dt: datetime | None
        try:
            last_dt = _db.from_apple_ns(int(last)) if last else None
            last_str = _db.format_local(last_dt) if last_dt else "no messages"
        except (ValueError, TypeError, OverflowError):
            last_dt = None
            last_str = "unknown"
        opened_dt: datetime | None = None
        opened_raw = row.get("last_read_message_timestamp")
        if opened_raw:
            try:
                opened_dt = _db.from_apple_ns(int(opened_raw))
            except (ValueError, TypeError, OverflowError):
                opened_dt = None
        kind = _conversation_kind(row.get("style"), row.get("chat_identifier"))
        identifier = neutralize_untrusted_text(str(row.get("chat_identifier") or ""))
        flags = ""
        if archived:
            flags += " [archived]"
        if filtered:
            flags += " [unknown sender]"
        if mentions:
            flags += " [mentions you]"
        counts = f"{row.get('message_count') or 0} message(s), {unread} unread"
        if opened_dt is not None:
            counts += f", {unseen} since last opened"
        lines.append(
            f"{len(lines) + 1}. [{kind}]{flags} "
            f"{neutralize_untrusted_text(str(name))} (chat ID: {identifier}) - "
            f"{counts}, last {last_str}",
        )
        items.append(
            {
                "chat_id": _output._json_text(row.get("chat_identifier")) or None,
                "name": _output._json_text(name),
                "kind": kind,
                "archived": archived,
                "filtered": filtered,
                "message_count": int(row.get("message_count") or 0),
                "unread_count": unread,
                "unseen_mentions": mentions,
                "unseen_since_last_opened": unseen if opened_dt is not None else None,
                "last_opened_at": _db.isoformat_local(opened_dt) if opened_dt else None,
                "last_message_at": _db.isoformat_local(last_dt) if last_dt else None,
            }
        )

    if as_json:
        return _output._json_dumps({"conversations": items})
    if not lines:
        return "No conversations with unread messages found."

    return f"Found {len(lines)} conversation(s):\n" + "\n".join(lines)
