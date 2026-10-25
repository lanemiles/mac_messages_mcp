# Copyright (c) 2023 Carter Lasalle
"""Attachment *content* search for the Messages database.

``attachments.search_attachments`` only matches attachment metadata (filename,
MIME type, dates). This module adds a best-effort full-text scan of the
attachment files themselves: plain text is read directly, PDFs are text
extracted with ``pypdf`` when installed, and images are OCR'd with
``pytesseract``/``tesseract`` when available.

Every step degrades gracefully: unreadable files are counted, unsupported
types are counted, and missing optional dependencies never raise. Output is
fenced through the untrusted-output boundary, like the rest of the suite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import attachments as _attachments
from . import contacts as _contacts
from . import db as _db
from . import extract as _extract
from . import search_index as _search_index
from .phone import canonical_handle
from .untrusted import bound_untrusted_output, neutralize_untrusted_text

# Longest snippet returned for a match, in characters.
_MAX_SNIPPET_CHARS = 200


def _resolve_contact_handles(contact: str) -> list[int] | None:
    """Resolve a contact selector to handle ROWIDs, or None when unknown.

    Mirrors ``attachments.search_attachments``: email handles are looked up
    case-insensitively, digit-containing inputs go through the phone resolver,
    and anything else is treated as a name and fuzzy matched.
    """
    contact = str(contact).strip()
    if "@" in contact:
        results = _db.query_messages_db(
            "SELECT ROWID FROM handle WHERE id = ? COLLATE NOCASE",
            (canonical_handle(contact) or contact.strip(),),
        )
        if results and "error" not in results[0]:
            return [r["ROWID"] for r in results]
        return None
    if any(c.isdigit() for c in contact):
        return _contacts._handles_for_address(contact) or None
    matches = _contacts.find_contact_by_name(contact)
    if not matches:
        return None
    resolved: list[int] = []
    for match in matches:
        handles = _contacts._handles_for_address(str(match["phone"])) or []
        resolved.extend(handles)
    return list(dict.fromkeys(resolved)) or None


def _build_search_query(
    start_date: str | None,
    end_date: str | None,
    contact: str | None,
    mime_type: str | None,
    fetch_limit: int,
    attachment_rowids: list[int] | None = None,
) -> tuple[str | None, tuple[Any, ...], str | None]:
    """Build the attachment-join query shared with ``search_attachments``.

    Returns ``(error, params, query)``. ``error`` is non-None for a bad date
    or an unresolvable contact.
    """
    where_clauses, date_params, date_error = _db.date_range_clauses(
        start_date, end_date
    )
    if date_error is not None:
        return date_error, (), None
    params: list[Any] = list(date_params)

    if mime_type:
        like_pattern = mime_type if "%" in mime_type else f"{mime_type}%"
        where_clauses.append("a.mime_type LIKE ?")
        params.append(like_pattern)

    if contact:
        handle_ids = _resolve_contact_handles(contact)
        if not handle_ids:
            return f"No handles found for contact '{contact}'.", (), None
        placeholders = ",".join(["?"] * len(handle_ids))
        where_clauses.append(f"m.handle_id IN ({placeholders})")
        params.extend(handle_ids)

    if attachment_rowids is not None:
        placeholders = ",".join(["?"] * len(attachment_rowids))
        where_clauses.append(f"a.ROWID IN ({placeholders})")
        params.extend(attachment_rowids)

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    query = f"""
        SELECT
            {_attachments.attachment_select_cols()},
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
    return None, (*params, fetch_limit), query


def _make_snippet(text: str, query: str) -> str:
    """Return at most ``_MAX_SNIPPET_CHARS`` centred on the first match.

    Leading/trailing ``...`` are added when the window is clipped from the
    surrounding text. Returns an empty string when the query is absent.
    """
    lowered = text.lower()
    needle = query.lower()
    index = lowered.find(needle)
    if index < 0:
        return ""

    window = _MAX_SNIPPET_CHARS
    lead = max(0, (window - len(needle)) // 2)
    start = max(0, index - lead)
    end = min(len(text), start + window)
    start = max(0, end - window)

    snippet = text[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


def _format_match_line(
    shaped: dict[str, Any],
    row: dict[str, Any],
    snippet: str,
) -> str:
    """Format one content match as the public single-line result."""
    filename = shaped.get("filename")
    if not filename and shaped.get("path"):
        filename = Path(shaped["path"]).name
    filename = neutralize_untrusted_text(filename or "(no name)")
    mime = neutralize_untrusted_text(shaped.get("mime_type") or "unknown")

    try:
        date_str = _db.format_local(_db.from_apple_ns(int(row["message_date"])))
    except (KeyError, ValueError, TypeError, OverflowError):
        date_str = "Unknown date"

    return (
        f"#{shaped['id']} {filename} [{mime}] "
        f"(message #{shaped['message_id']}, {date_str}) - {snippet}"
    )


def _format_skip_line(skipped: dict[str, int]) -> str | None:
    """Render the non-zero skip counts, or None when nothing was skipped."""
    parts: list[str] = []
    if skipped["missing"]:
        parts.append(f"{skipped['missing']} missing file(s)")
    if skipped["pdf_unsupported"]:
        parts.append(f"{skipped['pdf_unsupported']} pdf (pypdf not installed)")
    if skipped["ocr_unsupported"]:
        parts.append(f"{skipped['ocr_unsupported']} image (pytesseract not installed)")
    if skipped["unsupported_type"]:
        parts.append(f"{skipped['unsupported_type']} unsupported type(s)")
    if skipped["unreadable"]:
        parts.append(f"{skipped['unreadable']} unreadable file(s)")
    if not parts:
        return None

    line = "Skipped: " + ", ".join(parts) + "."
    hints: list[str] = []
    if skipped["pdf_unsupported"]:
        hints.append("Install PDF text support with: pip install pypdf.")
    if skipped["ocr_unsupported"]:
        hints.append(
            "Install OCR support with: pip install pytesseract and "
            "the tesseract binary.",
        )
    if hints:
        line += " " + " ".join(hints)
    return line


@bound_untrusted_output
def search_attachment_contents(
    query: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    contact: str | None = None,
    mime_type: str | None = None,
    limit: int = 20,
    max_file_bytes: int = 2_000_000,
) -> str:
    """Search attachment *contents* (not just metadata) for a substring.

    Text-like files are read directly, PDFs are text extracted with ``pypdf``
    when installed, and images are OCR'd when ``pytesseract`` and the
    tesseract binary are available. Missing files and unsupported types are
    counted and reported but never abort the search.

    Args:
        query: Case-insensitive substring to look for.
        start_date: Inclusive ISO date "YYYY-MM-DD" (UTC). Optional.
        end_date: Inclusive ISO date "YYYY-MM-DD" (UTC). Optional.
        contact: Phone number, email, or contact name. Optional.
        mime_type: Prefix match e.g. "image/" or "application/pdf". Optional.
        limit: Maximum number of matching attachments to return.
        max_file_bytes: Byte cap when reading text-like files.

    """
    if not query or not query.strip():
        return "Error: query cannot be empty."
    if limit <= 0:
        return "Error: limit must be positive."

    # Pull extra rows so filtering (stickers, plugin payloads, misses) can
    # still leave us with `limit` matches.
    fetch_limit = limit * 5
    # Prefer the attachment text index: it names the attachments whose stored
    # text mentions the query, so no file is opened at search time. The scan
    # below is the fallback when the index is disabled or cannot answer.
    index_hits = _search_index.candidate_attachment_rowids(query, fetch_limit)
    error, params, sql = _build_search_query(
        start_date,
        end_date,
        contact,
        mime_type,
        fetch_limit,
        attachment_rowids=index_hits,
    )
    if error is not None:
        return error

    if index_hits is not None and not index_hits:
        rows = []
    else:
        rows = _db.query_messages_db(sql, params)  # type: ignore[arg-type]
    if rows and "error" in rows[0]:
        return f"Error querying attachments: {rows[0]['error']}"

    rows = _attachments.filter_excluded_attachments(rows)

    safe_query = neutralize_untrusted_text(query)
    matches: list[str] = []
    skipped = {
        "missing": 0,
        "pdf_unsupported": 0,
        "ocr_unsupported": 0,
        "unsupported_type": 0,
        "unreadable": 0,
    }
    scanned = 0

    for row in rows:
        if len(matches) >= limit:
            break
        scanned += 1
        shaped = _attachments.shape_attachment(row)
        text = (
            _search_index.attachment_text(int(shaped["id"]))
            if index_hits is not None
            else None
        )
        if text is None:
            path = shaped.get("path")
            if not path or not shaped.get("exists"):
                skipped["missing"] += 1
                continue
            text, reason = _extract.extract_attachment_text(
                path,
                shaped.get("mime_type"),
                max_file_bytes,
            )
            if reason is not None:
                skipped[reason] += 1
                continue
        if not text or query.lower() not in text.lower():
            continue

        snippet = _make_snippet(text, query)
        matches.append(_format_match_line(shaped, row, snippet))

    lines: list[str] = []
    if matches:
        lines.append(f"Found {len(matches)} attachment(s) containing '{safe_query}':")
        lines.extend(matches)
    else:
        lines.append(
            f"No attachment contents matched '{safe_query}' in "
            f"{scanned} scanned attachment(s).",
        )

    skip_line = _format_skip_line(skipped)
    if skip_line is not None:
        lines.append(skip_line)
    if index_hits is not None:
        info = _search_index.status()
        pending = int(info.get("attachments_pending") or 0)
        if pending:
            lines.append(
                f"({pending} attachment(s) not yet downloaded from iCloud are not "
                "searchable until Messages fetches them.)"
            )

    lines.extend(
        (
            "",
            "Use tool_get_attachment(attachment_id=<id>) to fetch a specific file.",
        ),
    )
    return "\n".join(lines)
