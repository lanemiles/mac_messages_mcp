# Copyright (c) 2023 Carter Lasalle
"""Attachment metadata, search, and inline image fetches."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Image

from . import contacts as _contacts
from . import db as _db
from .phone import canonical_handle
from .untrusted import bound_untrusted_output, neutralize_untrusted_text

# UTI prefix that flags the iMessage app extension "balloon" payload format.
# These are container blobs (Apple Pay, polls, link previews, etc.) — not
# user-visible files, and parsing them is out of scope.
_PLUGIN_PAYLOAD_UTI_PREFIX = "com.apple.messages.MSMessageExtensionBalloonPlugin"


# MIME types we surface as inline image content rather than path metadata.
_INLINE_IMAGE_MIMES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/heic",
    "image/heif",
}


# Default cap on bytes returned inline. Above this we fall back to path
# metadata so we don't blow context on a 50MB video the model didn't ask for.
_DEFAULT_MAX_INLINE_BYTES = 5_000_000


def _resolve_attachment_path(filename: str | None) -> str | None:
    """Expand ~ and return an absolute path. Returns None for empty input."""
    if not filename:
        return None
    return str(Path(filename).expanduser())


def filter_excluded_attachments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop stickers and plugin-payload rows. Keeps everything else."""
    kept = []
    for row in rows:
        if row.get("is_sticker"):
            continue
        uti = row.get("uti") or ""
        if uti.startswith(_PLUGIN_PAYLOAD_UTI_PREFIX):
            continue
        transfer_name = row.get("transfer_name") or ""
        if transfer_name.endswith(".pluginPayloadAttachment"):
            continue
        kept.append(row)
    return kept


def shape_attachment(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw join row into the public metadata shape.

    chat.db's ``total_bytes`` is unreliable — many rows store a stale or
    rounded value (e.g. 1048576 for files that are actually 8MB). When the
    file is on disk we trust ``os.path.getsize`` instead.
    """
    path = _resolve_attachment_path(row.get("filename"))
    if path is None:
        size_bytes = row.get("total_bytes") or 0
        return {
            "id": row["attachment_id"],
            "message_id": row["message_id"],
            "filename": row.get("transfer_name"),
            "path": None,
            "mime_type": row.get("mime_type"),
            "uti": row.get("uti"),
            "size_bytes": size_bytes,
            "exists": False,
        }
    exists = Path(path).exists()
    if exists:
        try:
            size_bytes = Path(path).stat().st_size
        except OSError:
            size_bytes = row.get("total_bytes") or 0
    else:
        size_bytes = row.get("total_bytes") or 0
    return {
        "id": row["attachment_id"],
        "message_id": row["message_id"],
        "filename": row.get("transfer_name") or Path(path).name,
        "path": path,
        "mime_type": row.get("mime_type"),
        "uti": row.get("uti"),
        "size_bytes": size_bytes,
        "exists": exists,
    }


ATTACHMENT_SELECT_COLS = """
    a.ROWID AS attachment_id,
    maj.message_id AS message_id,
    a.filename AS filename,
    a.transfer_name AS transfer_name,
    a.mime_type AS mime_type,
    a.uti AS uti,
    a.total_bytes AS total_bytes,
    a.is_sticker AS is_sticker,
    a.hide_attachment AS hide_attachment,
    a.created_date AS created_date
"""


def _format_attachment_summary(attachments: list[dict[str, Any]]) -> str:
    """Compact one-line summary of a message's attachments — Tier 1 disclosure.

    Returns "" when the list is empty so callers can unconditionally append.
    The format keeps tokens low: id + mime_type per item, with the original
    filename only when distinct enough to be useful (small filenames help the
    agent disambiguate; we drop them past 3 attachments to cap the line).
    """
    if not attachments:
        return ""
    parts = []
    for att in attachments:
        mime = neutralize_untrusted_text(att.get("mime_type") or "?")
        # Filename helps when an agent is choosing between several attachments
        # on the same message; cap at 3 to keep the line short.
        if len(attachments) <= 3 and att.get("filename"):
            filename = neutralize_untrusted_text(att["filename"])
            parts.append(f"#{att['id']} {mime} ({filename})")
        else:
            parts.append(f"#{att['id']} {mime}")
    return f" [attachments: {', '.join(parts)}]"


def _format_attachment_line(
    shaped: dict[str, Any],
    date_str: str,
    sender: str,
) -> str:
    """Format one search_attachments row as a single summary line."""
    size_kb = (shaped["size_bytes"] or 0) / 1024
    marker = "" if shaped["exists"] else " [missing on disk]"
    return (
        f"- id={shaped['id']} msg={shaped['message_id']} "
        f"[{date_str}] from {sender} | "
        f"{shaped['mime_type'] or 'unknown'} | "
        f"{shaped['filename'] or '(no name)'} | "
        f"{size_kb:.1f} KB{marker}"
    )


def _attachments_for_message_ids(
    message_ids: list[int],
) -> dict[int, list[dict[str, Any]]]:
    """For a list of message ROWIDs, return attachment metadata per message.

    Messages with no surviving (post-filter) attachments are absent from
    the returned dict.
    """
    if not message_ids:
        return {}

    placeholders = ",".join(["?"] * len(message_ids))
    query = f"""
        SELECT
            {ATTACHMENT_SELECT_COLS},
            m.is_from_me AS is_from_me,
            m.handle_id AS handle_id
        FROM message_attachment_join maj
        JOIN attachment a ON a.ROWID = maj.attachment_id
        JOIN message m ON m.ROWID = maj.message_id
        WHERE maj.message_id IN ({placeholders})
        ORDER BY a.ROWID ASC
    """
    rows = _db.query_messages_db(query, tuple(message_ids))
    if rows and "error" in rows[0]:
        return {}

    # Drop rows that don't look like attachment-join rows. Two reasons to
    # keep this guard: (1) in tests where a single query_messages_db mock
    # serves multiple queries, foreign rows would crash shape_attachment;
    # (2) in production, a future schema change that adds an extra column
    # or strips one would degrade gracefully rather than break the tool.
    rows = [r for r in rows if "attachment_id" in r and "message_id" in r]
    rows = filter_excluded_attachments(rows)

    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        shaped = shape_attachment(row)
        grouped.setdefault(row["message_id"], []).append(shaped)
    return grouped


@bound_untrusted_output
def search_attachments(
    start_date: str | None = None,
    end_date: str | None = None,
    contact: str | None = None,
    mime_type: str | None = None,
    limit: int = 50,
    format: str = "text",
) -> str:
    """Search attachments across all messages by date range, contact, and MIME type.

    Returns metadata only — no file bytes. Use ``get_attachment(id)`` to fetch
    a specific file.

    Args:
        start_date: Inclusive local-day ISO date "YYYY-MM-DD". Optional.
        end_date: Inclusive local-day ISO date "YYYY-MM-DD". Optional.
        contact: Phone number, email, or contact name. Optional.
        mime_type: Prefix match e.g. "image/" or "application/pdf". Optional.
        limit: Maximum results to return (default 50).
        format: ``"text"`` or ``"json"`` (``{"attachments": [...]}``).

    """
    if format not in ("text", "json"):
        return "Error: format must be one of text, json."
    as_json = format == "json"
    if limit <= 0:
        return "Error: limit must be positive."

    date_clauses, date_params, date_error = _db.date_range_clauses(start_date, end_date)
    if date_error is not None:
        return date_error
    where_clauses: list[str] = list(date_clauses)
    params: list[Any] = list(date_params)

    if mime_type:
        # Caller can pass "image/" (prefix) or "image/jpeg" (exact). LIKE handles both.
        like_pattern = mime_type if "%" in mime_type else f"{mime_type}%"
        where_clauses.append("a.mime_type LIKE ?")
        params.append(like_pattern)

    handle_ids: list[int] | None = None
    if contact:
        contact = str(contact).strip()
        # Reuse the same resolution logic the existing tools use, minus the
        # interactive contact:N selection (this is a non-interactive search tool).
        if "@" in contact:
            # Same case folding as the message lookup, see the note there.
            results = _db.query_messages_db(
                "SELECT ROWID FROM handle WHERE id = ? COLLATE NOCASE",
                (canonical_handle(contact) or contact.strip(),),
            )
            if results and "error" not in results[0]:
                handle_ids = [r["ROWID"] for r in results]
        elif any(c.isdigit() for c in contact):
            handle_ids = _contacts.find_handles_by_phone(contact)
        else:
            matches = _contacts.find_contact_by_name(contact)
            if matches:
                resolved_handles: list[int] = []
                for m in matches:
                    h = _contacts.find_handles_by_phone(m["phone"]) or []
                    resolved_handles.extend(h)
                handle_ids = resolved_handles or None
        if not handle_ids:
            return f"No handles found for contact '{contact}'."
        placeholders = ",".join(["?"] * len(handle_ids))
        where_clauses.append(f"m.handle_id IN ({placeholders})")
        params.extend(handle_ids)

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    query = f"""
        SELECT
            {ATTACHMENT_SELECT_COLS},
            m.date AS message_date,
            m.is_from_me AS is_from_me,
            m.handle_id AS handle_id
        FROM message_attachment_join maj
        JOIN attachment a ON a.ROWID = maj.attachment_id
        JOIN message m ON m.ROWID = maj.message_id
        {where_sql}
        ORDER BY m.date DESC
        LIMIT ?
    """
    # Pull a few extra rows so post-filter we can still hit `limit` cleanly.
    fetch_limit = limit * 3
    rows = _db.query_messages_db(query, (*params, fetch_limit))

    if rows and "error" in rows[0]:
        if as_json:
            return json.dumps(
                {
                    "error": neutralize_untrusted_text(
                        rows[0]["error"], keep_newlines=True
                    )
                },
                ensure_ascii=False,
            )
        return f"Error querying attachments: {rows[0]['error']}"

    rows = filter_excluded_attachments(rows)
    rows = rows[:limit]

    if not rows:
        if as_json:
            return json.dumps({"attachments": []})
        return "No attachments found matching the given filters."

    _contacts.prime_contact_names(
        row.get("handle_id") for row in rows if not row.get("is_from_me")
    )
    lines = [f"Found {len(rows)} attachment(s):"]
    items: list[dict[str, Any]] = []
    for row in rows:
        shaped = shape_attachment(row)
        when = None
        try:
            when = _db.from_apple_ns(int(row["message_date"]))
            date_str = _db.format_local(when)
        except (ValueError, TypeError, OverflowError):
            date_str = "Unknown date"
        handle_id = row.get("handle_id")
        sender = (
            "You" if row.get("is_from_me") else _contacts.get_contact_name(handle_id)
        )
        lines.append(_format_attachment_line(shaped, date_str, sender))
        if as_json:
            text = lambda v: neutralize_untrusted_text(v, keep_newlines=True) or None
            sender_json: dict[str, Any] = {"name": text(sender)}
            if not row.get("is_from_me"):
                sender_json["handle_id"] = handle_id
                sender_json["address"] = text(_contacts.get_handle_address(handle_id))
            items.append(
                {
                    "id": shaped["id"],
                    "message_id": shaped["message_id"],
                    "date": _db.isoformat_local(when) if when else None,
                    "sender": sender_json,
                    "mime_type": text(shaped["mime_type"]),
                    "filename": text(shaped["filename"]),
                    "size_bytes": shaped["size_bytes"],
                    "exists": bool(shaped["exists"]),
                    "path": text(shaped["path"]),
                }
            )

    if as_json:
        return json.dumps({"attachments": items}, ensure_ascii=False)

    lines.extend(
        (
            "",
            "Use tool_get_attachment(attachment_id=<id>) to fetch a specific file.",
        ),
    )
    return "\n".join(lines)


def _convert_heic_to_png(heic_bytes: bytes) -> bytes:
    """Decode HEIC bytes and re-encode them as PNG (needs pillow-heif)."""
    import pillow_heif  # type: ignore[import-untyped]
    from PIL import Image as PILImage

    pillow_heif.register_heif_opener()
    image = PILImage.open(io.BytesIO(heic_bytes))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _heic_to_png_bytes(heic_bytes: bytes) -> bytes | None:
    """Convert HEIC bytes to PNG bytes.

    Returns None when conversion isn't available on this machine
    (pillow-heif not installed or libheif missing).
    """
    try:
        return _convert_heic_to_png(heic_bytes)
    except (ImportError, OSError, ValueError):
        return None


def _load_attachment_row(attachment_id: int) -> tuple[dict[str, Any] | None, str]:
    """Return ``(shaped_row, error_message)`` for one attachment ROWID.

    ``shaped_row`` is None when the query fails or the ROWID is absent, in
    which case ``error_message`` explains the failure; otherwise
    ``error_message`` is empty.
    """
    rows = _db.query_messages_db(
        f"""
        SELECT
            {ATTACHMENT_SELECT_COLS},
            m.is_from_me AS is_from_me,
            m.handle_id AS handle_id
        FROM message_attachment_join maj
        JOIN attachment a ON a.ROWID = maj.attachment_id
        JOIN message m ON m.ROWID = maj.message_id
        WHERE a.ROWID = ?
        LIMIT 1
        """,
        (attachment_id,),
    )

    if rows and "error" in rows[0]:
        return None, f"Error querying attachment: {rows[0]['error']}"
    if not rows:
        return None, f"Attachment {attachment_id} not found."
    return shape_attachment(rows[0]), ""


def _format_attachment_metadata(
    attachment_id: int,
    shaped: dict[str, Any],
) -> tuple[str, str | None]:
    """Return ``(summary_line, resolved_path)`` for a shaped attachment row.

    ``resolved_path`` is None when the row records no filename or the file is
    missing on disk, in which case ``summary_line`` explains why.
    """
    path = shaped["path"]
    mime = (shaped["mime_type"] or "").lower()

    if not path:
        return f"Attachment {attachment_id}: no filename recorded in database.", None

    if not Path(path).exists():
        return (
            f"Attachment {attachment_id} ({shaped['filename']}, {mime or 'unknown'}): "
            f"missing on disk at {path}",
            None,
        )

    size_kb = (shaped["size_bytes"] or Path(path).stat().st_size) / 1024
    return (
        f"Attachment {attachment_id}: {mime or 'unknown'} | "
        f"{shaped['filename']} | {size_kb:.1f} KB | path: {path}",
        path,
    )


def _describe_attachment(attachment_id: int) -> tuple[str, str | None]:
    """Summarize one attachment without reading its bytes.

    Returns ``(summary_line, resolved_path)``. ``resolved_path`` is None when
    the row is absent, records no filename, or the file is missing on disk;
    ``summary_line`` then carries the status or access error. Used by the
    terminal CLI, which prints the summary and may copy the file, without
    materializing inline image payloads.

    """
    shaped, error = _load_attachment_row(attachment_id)
    if shaped is None:
        return error, None
    return _format_attachment_metadata(attachment_id, shaped)


@bound_untrusted_output
def get_attachment(
    attachment_id: int,
    max_bytes: int = _DEFAULT_MAX_INLINE_BYTES,
):
    """Fetch a specific attachment by its ROWID.

    Always returns the resolved filesystem path so the human (or agent's
    filesystem tools) can act on the file directly — share it, save it,
    re-encode, attach elsewhere. Inline image bytes are a *bonus* added
    when the file is a supported image MIME type and fits under
    ``max_bytes``.

    Returns:
        - ``[metadata_text, Image]`` (list) for image MIME types under
          ``max_bytes`` (HEIC is converted to PNG inline if pillow-heif
          is available). The text always includes the absolute path.
        - ``str`` (metadata text only) for non-image types, missing files,
          oversized images, HEIC without pillow-heif, missing rows, and DB
          errors.

    """
    shaped, error = _load_attachment_row(attachment_id)
    if shaped is None:
        return error

    metadata_text, path = _format_attachment_metadata(attachment_id, shaped)
    if path is None:
        return metadata_text
    mime = (shaped["mime_type"] or "").lower()

    # Non-image: path-only return so the caller can read with their own tools.
    if mime not in _INLINE_IMAGE_MIMES:
        return (
            f"{metadata_text}\n"
            f"This is not an image. Use your filesystem read tools on the path above."
        )

    # Oversize image: path-only, no inline bytes.
    actual_size = Path(path).stat().st_size
    if actual_size > max_bytes:
        return (
            f"{metadata_text}\n"
            f"Image of {actual_size / 1024:.0f} KB exceeds max_bytes={max_bytes} — "
            f"inline render skipped. Read the file directly from path above, "
            f"or call again with a larger max_bytes."
        )

    raw = Path(path).read_bytes()

    if mime in {"image/heic", "image/heif"}:
        png = _heic_to_png_bytes(raw)
        if png is None:
            return (
                f"{metadata_text}\n"
                f"HEIC image but pillow-heif is not available for conversion. "
                f"Install pillow-heif or read the file directly from path above."
            )
        return [metadata_text, Image(data=png, format="png")]

    fmt = mime.split("/", 1)[1] if "/" in mime else "png"
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in {"jpeg", "png", "gif", "webp"}:
        fmt = "png"
    return [metadata_text, Image(data=raw, format=fmt)]
