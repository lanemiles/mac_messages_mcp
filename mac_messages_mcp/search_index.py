# Copyright (c) 2023 Carter Lasalle
"""A local FTS5 index over message bodies, kept current incrementally.

``fuzzy_search_messages`` ranks candidates in Python, so it can only look at a
bounded number of rows per call: the newest few thousand. On modern macOS most
bodies live only in ``attributedBody``, which SQL cannot search, so every row
is a candidate and anything older than that window is never found. This module
indexes decoded bodies once, then only rows with a ROWID above the last one
indexed, and answers "which ROWIDs mention these words" across all history in
milliseconds. The fuzzy pass still re-ranks the hits.

The index is derived data: a copy of message text in a private (0600) SQLite
file under ``~/Library/Application Support/mac-messages-mcp/``. It is rebuilt
from scratch when it refers to a different chat.db than the one being read.
``MAC_MESSAGES_SEARCH_INDEX`` overrides the path; ``none`` disables the index,
in which case search falls back to the bounded scan.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from . import db as _db

logger = logging.getLogger(__name__)

INDEX_ENV_VAR = "MAC_MESSAGES_SEARCH_INDEX"
_DEFAULT_INDEX = "~/Library/Application Support/mac-messages-mcp/search_index.sqlite"
_BATCH = 5_000
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

_lock = threading.RLock()
_connection: sqlite3.Connection | None = None
_connection_path: Path | None = None


def index_path() -> Path | None:
    """Where the index lives; None when disabled."""
    raw = os.environ.get(INDEX_ENV_VAR)
    if raw is not None and raw.strip().lower() in {"none", "off", ""}:
        return None
    return Path(raw if raw else _DEFAULT_INDEX).expanduser()


def enabled() -> bool:
    return index_path() is not None


def reset() -> None:
    """Close the index connection (tests, or after the path changes)."""
    global _connection, _connection_path
    with _lock:
        if _connection is not None:
            try:
                _connection.close()
            except sqlite3.Error:
                pass
        _connection = None
        _connection_path = None


def _connect(path: Path) -> sqlite3.Connection:
    global _connection, _connection_path
    if _connection is not None and _connection_path == path:
        return _connection
    reset()
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    conn = sqlite3.connect(path)
    if not existed:
        os.chmod(path, 0o600)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
        "body, tokenize = 'unicode61 remove_diacritics 2')"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS attachments_fts USING fts5("
        "body, tokenize = 'unicode61 remove_diacritics 2')"
    )
    # Attachments whose file was not on disk when first seen (not yet
    # downloaded from iCloud); retried on every refresh.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS attachments_pending (rowid INTEGER PRIMARY KEY)"
    )
    conn.commit()
    _connection = conn
    _connection_path = path
    return conn


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _source_identity() -> str:
    db_path = _db.get_messages_db_path()
    try:
        stat = os.stat(db_path)
        return f"{db_path}|{stat.st_ino}"
    except OSError:
        return db_path


def _displayable_body(row: dict[str, Any]) -> str | None:
    # Local import: read depends on this module's caller, not the reverse.
    from .read import _message_body

    if row.get("associated_message_type"):
        return None  # tapbacks carry no words of their own
    body = _message_body(row)
    if not body or body in ("[attachment]", "(reaction)", "(unsent message)"):
        return None
    return body


def refresh() -> dict[str, Any] | None:
    """Bring the index up to date with chat.db. Returns status, or None if disabled."""
    path = index_path()
    if path is None:
        return None
    with _lock:
        conn = _connect(path)
        identity = _source_identity()
        if _meta(conn, "source") != identity:
            conn.execute("DELETE FROM messages_fts")
            conn.execute("DELETE FROM attachments_fts")
            conn.execute("DELETE FROM attachments_pending")
            _set_meta(conn, "source", identity)
            _set_meta(conn, "last_rowid", "0")
            _set_meta(conn, "last_attachment_rowid", "0")
            conn.commit()
        last_rowid = int(_meta(conn, "last_rowid") or 0)
        while True:
            rows = _db.query_messages_db(
                "SELECT ROWID, text, attributedBody, date_retracted, "
                "associated_message_type FROM message WHERE ROWID > ? "
                "ORDER BY ROWID LIMIT ?",
                (last_rowid, _BATCH),
            )
            if not rows or "error" in rows[0]:
                if rows and "error" in rows[0]:
                    logger.warning("Search index refresh stopped: %s", rows[0]["error"])
                break
            batch = []
            for row in rows:
                rowid = row.get("ROWID")
                if rowid is None:
                    continue
                body = _displayable_body(row)
                if body:
                    batch.append((int(rowid), body))
                last_rowid = max(last_rowid, int(rowid))
            if batch:
                conn.executemany(
                    "INSERT OR REPLACE INTO messages_fts (rowid, body) VALUES (?, ?)",
                    batch,
                )
            _set_meta(conn, "last_rowid", str(last_rowid))
            conn.commit()
            if len(rows) < _BATCH:
                break
        return status()


def status() -> dict[str, Any]:
    """Row counts, last indexed ROWIDs and path; ``enabled`` False when off."""
    path = index_path()
    if path is None:
        return {"enabled": False}
    with _lock:
        conn = _connect(path)
        indexed = conn.execute("SELECT count(*) FROM messages_fts").fetchone()[0]
        attachments = conn.execute("SELECT count(*) FROM attachments_fts").fetchone()[0]
        pending = conn.execute("SELECT count(*) FROM attachments_pending").fetchone()[0]
        return {
            "enabled": True,
            "path": str(path),
            "indexed": int(indexed),
            "last_rowid": int(_meta(conn, "last_rowid") or 0),
            "attachments_indexed": int(attachments),
            "attachments_pending": int(pending),
            "last_attachment_rowid": int(_meta(conn, "last_attachment_rowid") or 0),
        }


# ---------------------------------------------------------------------------
# Attachment text
# ---------------------------------------------------------------------------

_ATTACHMENT_BATCH = 500
_ATTACHMENT_MAX_BYTES = 2_000_000


def _attachment_rows(last_rowid: int) -> list[dict[str, Any]]:
    rows = _db.query_messages_db(
        "SELECT ROWID, filename, mime_type, transfer_name, is_sticker, uti "
        "FROM attachment WHERE ROWID > ? ORDER BY ROWID LIMIT ?",
        (last_rowid, _ATTACHMENT_BATCH),
    )
    if not rows or "error" in rows[0]:
        return []
    return [r for r in rows if r.get("ROWID") is not None and "filename" in r]


def _index_attachment_row(conn: sqlite3.Connection, row: dict[str, Any]) -> str:
    """Index one attachment; returns 'indexed', 'pending', or 'skipped'."""
    from . import attachments as _attachments
    from . import extract as _extract

    rowid = int(row["ROWID"])
    if row.get("is_sticker") or (row.get("uti") or "").startswith(
        _attachments._PLUGIN_PAYLOAD_UTI_PREFIX
    ):
        return "skipped"
    path = _attachments._resolve_attachment_path(row.get("filename"))
    if not path:
        return "skipped"
    if not os.path.exists(path):
        conn.execute(
            "INSERT OR IGNORE INTO attachments_pending (rowid) VALUES (?)", (rowid,)
        )
        return "pending"
    text, reason = _extract.extract_attachment_text(
        path, row.get("mime_type"), _ATTACHMENT_MAX_BYTES
    )
    if reason is not None or not text or not text.strip():
        return "skipped"
    conn.execute(
        "INSERT OR REPLACE INTO attachments_fts (rowid, body) VALUES (?, ?)",
        (rowid, text),
    )
    return "indexed"


def refresh_attachments() -> None:
    """Index text of attachments newer than the last one seen, and retry pending."""
    path = index_path()
    if path is None:
        return
    with _lock:
        conn = _connect(path)
        # Retry files that were not downloaded when first seen.
        pending = [r[0] for r in conn.execute("SELECT rowid FROM attachments_pending")]
        if pending:
            placeholders = ", ".join("?" for _ in pending)
            rows = _db.query_messages_db(
                "SELECT ROWID, filename, mime_type, transfer_name, is_sticker, uti "
                f"FROM attachment WHERE ROWID IN ({placeholders})",
                tuple(pending),
            )
            if rows and "error" not in rows[0]:
                for row in rows:
                    if row.get("ROWID") is None or "filename" not in row:
                        continue
                    outcome = _index_attachment_row(conn, row)
                    if outcome != "pending":
                        conn.execute(
                            "DELETE FROM attachments_pending WHERE rowid = ?",
                            (int(row["ROWID"]),),
                        )
            conn.commit()
        last = int(_meta(conn, "last_attachment_rowid") or 0)
        rows = _attachment_rows(last)
        for row in rows:
            _index_attachment_row(conn, row)
            last = max(last, int(row["ROWID"]))
        _set_meta(conn, "last_attachment_rowid", str(last))
        conn.commit()


def candidate_attachment_rowids(term: str, limit: int) -> list[int] | None:
    """Attachment ROWIDs whose extracted text contains every word of ``term``.

    None when the index is disabled or cannot answer (fall back to a scan).
    """
    path = index_path()
    if path is None:
        return None
    query = _fts_query(term)
    if query is None:
        return None
    try:
        refresh()
        refresh_attachments()
        with _lock:
            conn = _connect(path)
            rows = conn.execute(
                "SELECT rowid FROM attachments_fts WHERE attachments_fts MATCH ? "
                "ORDER BY bm25(attachments_fts) LIMIT ?",
                (query, limit),
            ).fetchall()
        return [int(r[0]) for r in rows]
    except sqlite3.Error as exc:
        logger.warning("Attachment index unavailable (%s); falling back to a scan", exc)
        return None


def attachment_text(rowid: int) -> str | None:
    """The indexed text of one attachment, or None if it is not indexed."""
    path = index_path()
    if path is None:
        return None
    try:
        with _lock:
            conn = _connect(path)
            row = conn.execute(
                "SELECT body FROM attachments_fts WHERE rowid = ?", (rowid,)
            ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _fts_query(term: str) -> str | None:
    """Every word as a quoted prefix term, ANDed. None when there are no words."""
    tokens = _TOKEN_RE.findall(term)
    if not tokens:
        return None
    return " ".join('"' + t.replace('"', '""') + '"*' for t in tokens)


def candidate_rowids(term: str, limit: int) -> list[int] | None:
    """ROWIDs whose bodies contain every word of ``term``, best first.

    None means the index could not answer (disabled, no searchable words, or
    an error) and the caller should use its bounded scan instead.
    """
    path = index_path()
    if path is None:
        return None
    query = _fts_query(term)
    if query is None:
        return None
    try:
        refresh()
        with _lock:
            conn = _connect(path)
            rows = conn.execute(
                "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ? "
                "ORDER BY bm25(messages_fts) LIMIT ?",
                (query, limit),
            ).fetchall()
        return [int(r[0]) for r in rows]
    except sqlite3.Error as exc:
        logger.warning("Search index unavailable (%s); falling back to a scan", exc)
        return None


def summary_line() -> str | None:
    """One line for the access diagnostic, or None when disabled."""
    try:
        info = status()
    except sqlite3.Error as exc:
        return f"Search index: unavailable ({exc})"
    if not info.get("enabled"):
        return "Search index: disabled"
    line = (
        f"Search index: {info['indexed']} messages indexed, "
        f"{info['attachments_indexed']} attachments indexed"
    )
    if info.get("attachments_pending"):
        line += f" ({info['attachments_pending']} awaiting download)"
    return f"{line} at {info['path']}"
