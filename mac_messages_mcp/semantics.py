# Copyright (c) 2023 Carter Lasalle
"""Per-message semantics: bodies, tags, tapbacks, replies, events, effects.

Pure helpers over a message row (plus batched lookups for the bodies that
tapbacks and replies point at). The read path builds records from these.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from . import contacts as _contacts
from . import db as _db
from .typedstream import extract_body_from_attributed
from .untrusted import neutralize_untrusted_text

logger = logging.getLogger(__name__)


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


_URL_BALLOON = "com.apple.messages.URLBalloonProvider"


_QUOTE_CHARS = 60


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


def _effect_name(style_id: Any) -> str | None:
    """``impact`` for ``com.apple.MobileSMS.expressivesend.impact``, etc."""
    if not style_id:
        return None
    text = str(style_id)
    return text.rsplit(".", 1)[-1] or text


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
    balloon: dict[str, Any] | None = None,
    edits: dict[str, Any] | None = None,
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
        previous = None
        if edits and len(edits["edits"]) >= 2:
            previous = edits["edits"][-2]["body"]
        tags.append(
            f'edited; was: "{neutralize_untrusted_text(_quote(previous))}"'
            if previous
            else "edited"
        )
    if row.get("date_retracted"):
        tags.append("unsent")
    if row.get("subject"):
        tags.append(
            f"subject: {neutralize_untrusted_text(_quote(str(row['subject'])))}"
        )
    effect = _effect_name(row.get("expressive_send_style_id"))
    if effect:
        tags.append(f"effect: {neutralize_untrusted_text(effect)}")
    if row.get("is_audio_message"):
        tags.append("audio message")
        if row.get("date_played"):
            tags.append("played")
    if row.get("is_time_sensitive"):
        tags.append("time sensitive")
    if row.get("was_delivered_quietly"):
        tags.append("delivered quietly")
    is_link, app = _balloon_label(row.get("balloon_bundle_id"))
    if is_link:
        title = balloon.get("title") if balloon else None
        tags.append(
            f'link preview: "{neutralize_untrusted_text(_quote(title))}"'
            if title
            else "link preview"
        )
    elif app:
        details = (balloon or {}).get("details") or []
        shown = "; ".join(neutralize_untrusted_text(d) for d in details[:2])
        tags.append(
            f"app: {neutralize_untrusted_text(app)}" + (f" — {shown}" if shown else "")
        )
    if event is not None:
        tags.append("event")
    if row.get("has_unseen_mention"):
        tags.append("mentions you")
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
