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
            _set_meta(conn, "source", identity)
            _set_meta(conn, "last_rowid", "0")
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
    """Row count, last indexed ROWID and path; ``enabled`` False when off."""
    path = index_path()
    if path is None:
        return {"enabled": False}
    with _lock:
        conn = _connect(path)
        indexed = conn.execute("SELECT count(*) FROM messages_fts").fetchone()[0]
        return {
            "enabled": True,
            "path": str(path),
            "indexed": int(indexed),
            "last_rowid": int(_meta(conn, "last_rowid") or 0),
        }


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
    return f"Search index: {info['indexed']} messages indexed at {info['path']}"
