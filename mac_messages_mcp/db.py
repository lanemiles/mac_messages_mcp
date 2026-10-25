# Copyright (c) 2023 Carter Lasalle
"""Read-only access to the Messages database: connections, queries, Apple-epoch
timestamps, date filters, and the access diagnostic."""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

_FULL_DISK_ACCESS_HINT = (
    "PLEASE TELL THE USER TO GRANT FULL DISK ACCESS TO THE TERMINAL "
    "APPLICATION(CURSOR, TERMINAL, CLAUDE, ETC.) AND RESTART THE "
    "APPLICATION. DO NOT RETRY UNTIL NEXT MESSAGE."
)


def _connect_sqlite_readonly(path: str) -> sqlite3.Connection:
    """Open a local SQLite database without write, journal, or creation access."""
    uri = f"{Path(path).expanduser().resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def get_messages_db_path() -> str:
    """Get the path to the Messages database."""
    # Resolve through os.path.expanduser rather than Path("~").expanduser():
    # pathlib only routes bare "~" through os.path from 3.12 on; on 3.10/3.11
    # it reads $HOME itself, so the documented expanduser seam (and any HOME
    # override hook) has no effect.
    return str(Path(os.path.expanduser("~")) / "Library/Messages/chat.db")


# One read-only connection per thread, reused across queries. Opening a
# connection to a multi-gigabyte chat.db is the dominant cost of a small
# query, and a page of messages used to open one per rendered line. The
# cache is keyed by the file's identity, so a replaced or relocated database
# gets a fresh connection rather than a stale handle.
_LOCAL = threading.local()


def _db_identity(db_path: str) -> tuple[str, int] | None:
    try:
        stat = os.stat(db_path)
    except OSError:
        return None
    return (db_path, stat.st_ino)


def _cached_connection(db_path: str) -> sqlite3.Connection:
    """Return this thread's connection to ``db_path``, opening it if needed."""
    identity = _db_identity(db_path)
    cached = getattr(_LOCAL, "connection", None)
    if cached is not None and getattr(_LOCAL, "identity", None) == identity:
        return cached
    close_cached_connections()
    conn = _connect_sqlite_readonly(db_path)
    conn.row_factory = sqlite3.Row
    _LOCAL.connection = conn
    _LOCAL.identity = identity
    return conn


def close_cached_connections() -> None:
    """Drop this thread's cached connection (tests, or after an I/O error)."""
    cached = getattr(_LOCAL, "connection", None)
    if cached is not None:
        try:
            cached.close()
        except sqlite3.Error:
            pass
    _LOCAL.connection = None
    _LOCAL.identity = None


def query_messages_db(query: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Query the Messages database and return results as a list of dictionaries."""
    db_path = get_messages_db_path()

    # Check if the database file exists and is accessible
    if not Path(db_path).exists():
        close_cached_connections()
        return [{"error": f"Messages database not found at {db_path}"}]

    # Try to connect to the database
    try:
        conn = _cached_connection(db_path)
    except (sqlite3.Error, OSError) as e:
        close_cached_connections()
        return [
            {
                "error": (
                    "Cannot access Messages database. Please grant Full Disk "
                    "Access permission to your terminal application in System "
                    "Preferences > Security & Privacy > Privacy > Full Disk "
                    f"Access. Error: {e!s} {_FULL_DISK_ACCESS_HINT}"
                ),
            },
        ]

    try:
        cursor = conn.cursor()
        cursor.execute(query, params)
        results = [dict(row) for row in cursor.fetchall()]
    except sqlite3.ProgrammingError as e:
        # The connection itself is unusable (closed underneath us); drop it so
        # the next query reconnects instead of failing forever.
        close_cached_connections()
        return [{"error": str(e)}]
    except (sqlite3.Error, ValueError, TypeError) as e:
        return [{"error": str(e)}]
    return results


def local_timezone() -> tzinfo:
    """The timezone this Mac displays times in; filters and output both use it."""
    return datetime.now().astimezone().tzinfo or timezone.utc


def _parse_iso_date(value: str) -> datetime | None:
    """Parse an inclusive "YYYY-MM-DD" day at local midnight, or return None."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=local_timezone())
    except ValueError:
        return None


def format_local(moment: datetime) -> str:
    """Render a timestamp the way message lines show it, in local time."""
    return moment.astimezone(local_timezone()).strftime("%Y-%m-%d %H:%M:%S")


def isoformat_local(moment: datetime) -> str:
    """ISO 8601 with the local UTC offset, for JSON output."""
    return moment.astimezone(local_timezone()).isoformat()


def date_range_clauses(
    start_date: str | None,
    end_date: str | None,
) -> tuple[list[str], list[str], str | None]:
    """Build Apple-ns date-range SQL clauses for a message query.

    Returns ``(clauses, params, error)``. Both bounds are inclusive days in the
    Mac's local timezone, written "YYYY-MM-DD", so they agree with the local
    timestamps the output shows; the end bound is exclusive at the following
    local midnight.
    Timestamps are string-bound so a seconds-format row cannot overflow SQLite's
    integer comparison.
    """
    clauses: list[str] = []
    params: list[str] = []
    if start_date:
        parsed = _parse_iso_date(start_date)
        if parsed is None:
            return (
                [],
                [],
                f"Error: start_date must be YYYY-MM-DD, got '{start_date}'.",
            )
        clauses.append("CAST(m.date AS TEXT) >= ?")
        params.append(str(to_apple_ns(parsed)))
    if end_date:
        parsed = _parse_iso_date(end_date)
        if parsed is None:
            return (
                [],
                [],
                f"Error: end_date must be YYYY-MM-DD, got '{end_date}'.",
            )
        clauses.append("CAST(m.date AS TEXT) < ?")
        params.append(str(to_apple_ns(parsed + timedelta(days=1))))
    return clauses, params, None


APPLE_EPOCH_OFFSET = 978307200  # seconds between the unix epoch and 2001-01-01


def _escape_like(term: str) -> str:
    """Escape SQL LIKE wildcards so the term is matched literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _unreadable_file_error(db_path: str) -> str | None:
    """Return an error message when db_path cannot be read, else None."""
    try:
        with Path(db_path).open("rb") as f:
            # Just try to read a byte to confirm access
            f.read(1)
    except PermissionError:
        return (
            f"ERROR: Permission denied when trying to read {db_path}. "
            "Please grant Full Disk Access permission to your terminal "
            f"application. {_FULL_DISK_ACCESS_HINT}"
        )
    except OSError as e:
        return f"ERROR: Unknown error reading file: {e!s} {_FULL_DISK_ACCESS_HINT}"
    return None


def _describe_messages_schema(db_path: str) -> list[str]:
    """Connect to db_path and report the table counts the access check shows."""
    conn = _connect_sqlite_readonly(db_path)
    lines = ["Successfully connected to database"]
    cursor = conn.cursor()
    cursor.execute("SELECT count(*) FROM sqlite_master")
    lines.append(f"Database contains {cursor.fetchone()[0]} tables")
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('message', 'handle', 'chat')",
    )
    tables = [row[0] for row in cursor.fetchall()]
    if "message" in tables and "handle" in tables:
        lines.append("Required tables (message, handle) are present")
    else:
        lines.append(
            f"WARNING: Some required tables are missing. Found: {', '.join(tables)}",
        )
    conn.close()
    return lines


def check_messages_db_access() -> str:
    """Check if the Messages database is accessible and return detailed information."""
    db_path = get_messages_db_path()

    # Check if the file exists
    if not Path(db_path).exists():
        return (
            f"ERROR: Messages database not found at {db_path} "
            f"{_FULL_DISK_ACCESS_HINT}"
        )

    status = [f"Database file exists at: {db_path}"]

    # Check file permissions
    read_error = _unreadable_file_error(db_path)
    if read_error is not None:
        return read_error
    status.append("File is readable")

    # Try to connect to the database
    try:
        status.extend(_describe_messages_schema(db_path))
    except sqlite3.Error as e:
        return f"ERROR: Database connection error: {e!s} {_FULL_DISK_ACCESS_HINT}"

    from . import policy  # local import: policy depends on phone, not on db
    from . import search_index

    status.extend(policy.summary_lines())
    index_line = search_index.summary_line()
    if index_line:
        status.append(index_line)
    return "\n".join(status)


_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def to_apple_ns(dt: datetime) -> int:
    """Convert a UTC datetime to Apple's nanoseconds-since-2001 format."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int((dt - _APPLE_EPOCH).total_seconds() * 1_000_000_000)


def from_apple_ns(ts: int) -> datetime:
    """Convert an Apple-epoch ``message.date`` value to a UTC datetime.

    Older chat.db rows store seconds-since-2001 (10 or fewer digits in the
    integer); newer macOS versions store nanoseconds (more than 10 digits).
    Both are handled here so callers don't repeat the heuristic.
    """
    seconds = ts / 1_000_000_000 if len(str(ts)) > 10 else ts
    return _APPLE_EPOCH + timedelta(seconds=seconds)
