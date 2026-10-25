# Copyright (c) 2023 Carter Lasalle
"""Schema-faithful Messages and AddressBook databases for tests.

The DDL under ``tests/fixtures/`` was dumped from a real macOS ``chat.db`` and
``AddressBook-v22.abcddb`` (``sqlite3 -readonly ... .schema``), so a query that
names a column or table that does not exist fails here the way it would on a
user's Mac, instead of passing against a mocked ``query_messages_db``.

The code under test opens its own read-only connections to these files. Only
the fixture's writer connection runs the triggers, so the Apple-private SQL
functions those triggers call (``verify_chat``, ``delete_attachment_path``, ...)
are registered as no-ops on the writer alone.
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"
CHAT_SCHEMA = FIXTURES / "chat_schema.sql"
ADDRESSBOOK_SCHEMA = FIXTURES / "addressbook_schema.sql"
ATTRIBUTED_BODY_SAMPLE = FIXTURES / "attributed_body_sample.hex"

# What the sample blob's payload decodes to: Apple's U+FFFC attachment
# placeholder followed by the (substituted) sentence. See the fixture header.
ATTACHMENT_PLACEHOLDER = "\ufffc"
SAMPLE_BODY_TEXT = (
    "fixture probe 2026: this sentence stands in for a real text that had a "
    "photo attached to it!......"
)


def sample_attributed_body() -> bytes:
    """A real macOS 27 ``attributedBody`` blob (text + one attachment)."""
    lines = ATTRIBUTED_BODY_SAMPLE.read_text().splitlines()
    return bytes.fromhex("".join(line for line in lines if not line.startswith("#")))


def attachment_only_attributed_body() -> bytes:
    """The sample blob with its payload reduced to just the U+FFFC placeholder.

    This is what Messages stores for an attachment-only message: no words,
    only the object-replacement character standing in for the file.
    """
    blob = sample_attributed_body()
    marker = blob.find(b"NSString") + len(b"NSString")
    length_pos = marker + 5
    payload_start = length_pos + 1
    payload_len = blob[length_pos]
    placeholder = ATTACHMENT_PLACEHOLDER.encode()
    return (
        blob[:length_pos]
        + bytes([len(placeholder)])
        + placeholder
        + blob[payload_start + payload_len :]
    )


# Functions Messages.app registers on its connection; the dumped triggers call
# them. Stubbed so inserts and deletes in tests fire the real triggers.
_APPLE_SQL_FUNCTIONS = (
    "verify_chat",
    "guid_for_chat",
    "is_mic_enabled",
    "delete_attachment_path",
    "before_delete_attachment_path",
    "after_delete_message_plugin",
    "delete_chat_background_before_deleting_chat",
)

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def apple_ns(when: datetime | None = None) -> int:
    """Apple-epoch nanoseconds for ``when`` (default: now), as chat.db stores."""
    moment = when or datetime.now(timezone.utc)
    return int((moment - _APPLE_EPOCH).total_seconds() * 1_000_000_000)


def _statements(ddl: str) -> list[str]:
    """Split a ``.schema`` dump into statements (triggers span several lines)."""
    statements: list[str] = []
    buffer: list[str] = []
    for line in ddl.splitlines():
        if not line.strip() or line.lstrip().startswith("--"):
            continue
        buffer.append(line)
        if sqlite3.complete_statement("\n".join(buffer)):
            statements.append("\n".join(buffer))
            buffer = []
    return statements


def load_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    """Create every object from a ``.schema`` dump on ``conn``.

    ``sqlite_sequence`` is skipped: SQLite creates it itself for AUTOINCREMENT
    and refuses a user-issued CREATE for it.
    """
    for statement in _statements(schema_path.read_text()):
        head = statement.split("(", 1)[0]
        if "sqlite_sequence" in head:
            continue
        conn.execute(statement)
    conn.commit()


class ChatDb:
    """A real-schema ``chat.db`` the tests write and the code reads.

    Pass ``template`` (an empty database built earlier by this class) to copy
    it instead of re-running the 100+ DDL statements; the conftest does this
    once per session.
    """

    def __init__(self, path: Path, *, template: Path | None = None) -> None:
        self.path = path
        if template is not None:
            shutil.copyfile(template, path)
        self.conn = sqlite3.connect(path)
        for name in _APPLE_SQL_FUNCTIONS:
            self.conn.create_function(name, -1, lambda *_args: None)
        if template is None:
            load_schema(self.conn, CHAT_SCHEMA)
        self._guid = 0
        self._autocommit = True

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def batch(self):
        """Defer commits until the block ends (bulk fixtures)."""
        self._autocommit = False
        try:
            yield self
            self.conn.commit()
        finally:
            self._autocommit = True

    def _commit(self) -> None:
        if self._autocommit:
            self.conn.commit()

    def _next_guid(self, prefix: str) -> str:
        self._guid += 1
        return f"{prefix}-{self._guid:04d}"

    def add_handle(self, address: str, service: str = "iMessage") -> int:
        cur = self.conn.execute(
            "INSERT INTO handle (id, service) VALUES (?, ?)", (address, service)
        )
        self._commit()
        return int(cur.lastrowid)

    def add_chat(
        self,
        chat_identifier: str,
        *,
        style: int = 45,
        display_name: str | None = None,
        handles: tuple[int, ...] = (),
        service_name: str = "iMessage",
    ) -> int:
        """Insert a chat row. ``style`` 45 is a 1:1 thread, 43 a group."""
        marker = "+" if style == 43 else "-"
        room_name = chat_identifier if style == 43 else None
        cur = self.conn.execute(
            "INSERT INTO chat (guid, style, chat_identifier, service_name, "
            "room_name, display_name) VALUES (?, ?, ?, ?, ?, ?)",
            (
                f"{service_name};{marker};{chat_identifier}",
                style,
                chat_identifier,
                service_name,
                room_name,
                display_name,
            ),
        )
        chat_id = int(cur.lastrowid)
        for handle_id in handles:
            self.conn.execute(
                "INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (?, ?)",
                (chat_id, handle_id),
            )
        self._commit()
        return chat_id

    def add_message(
        self,
        text: str | None,
        *,
        handle_id: int = 0,
        is_from_me: int = 0,
        chat_id: int | None = None,
        rowid: int | None = None,
        date: int | None = None,
        service: str = "iMessage",
        **columns: Any,
    ) -> int:
        """Insert a message, optionally joined to ``chat_id``. Returns ROWID."""
        when = apple_ns() if date is None else date
        values: dict[str, Any] = {
            "guid": self._next_guid("msg"),
            "text": text,
            "handle_id": handle_id,
            "is_from_me": is_from_me,
            "date": when,
            "service": service,
            **columns,
        }
        if rowid is not None:
            values["ROWID"] = rowid
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cur = self.conn.execute(
            f"INSERT INTO message ({cols}) VALUES ({marks})", tuple(values.values())
        )
        message_id = int(cur.lastrowid)
        if chat_id is not None:
            self.conn.execute(
                "INSERT INTO chat_message_join (chat_id, message_id, message_date) "
                "VALUES (?, ?, ?)",
                (chat_id, message_id, when),
            )
        self._commit()
        return message_id

    def add_attachment(
        self,
        message_id: int,
        filename: str | None,
        mime_type: str | None,
        *,
        transfer_name: str | None = None,
        uti: str | None = None,
        total_bytes: int = 0,
        is_sticker: int = 0,
        transfer_state: int = 5,
        **columns: Any,
    ) -> int:
        guid = self._next_guid("att")
        values: dict[str, Any] = {
            "guid": guid,
            "original_guid": guid,
            "filename": filename,
            "mime_type": mime_type,
            "transfer_name": transfer_name
            or (Path(filename).name if filename else None),
            "uti": uti,
            "total_bytes": total_bytes,
            "is_sticker": is_sticker,
            "transfer_state": transfer_state,
            **columns,
        }
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cur = self.conn.execute(
            f"INSERT INTO attachment ({cols}) VALUES ({marks})", tuple(values.values())
        )
        attachment_id = int(cur.lastrowid)
        self.conn.execute(
            "INSERT INTO message_attachment_join (message_id, attachment_id) "
            "VALUES (?, ?)",
            (message_id, attachment_id),
        )
        self._commit()
        return attachment_id


class AddressBookDb:
    """A real-schema ``AddressBook-v22.abcddb`` holding the contact tables."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        load_schema(self.conn, ADDRESSBOOK_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def add_contact(
        self,
        first_name: str | None,
        last_name: str | None,
        *,
        phones: tuple[str, ...] = (),
        emails: tuple[str, ...] = (),
        nickname: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO ZABCDRECORD (ZFIRSTNAME, ZLASTNAME, ZNICKNAME) "
            "VALUES (?, ?, ?)",
            (first_name, last_name, nickname),
        )
        owner = int(cur.lastrowid)
        for index, number in enumerate(phones):
            self.conn.execute(
                "INSERT INTO ZABCDPHONENUMBER (ZOWNER, ZFULLNUMBER, ZORDERINGINDEX) "
                "VALUES (?, ?, ?)",
                (owner, number, index),
            )
        for address in emails:
            self.conn.execute(
                "INSERT INTO ZABCDEMAILADDRESS (ZOWNER, ZADDRESS) VALUES (?, ?)",
                (owner, address),
            )
        self.conn.commit()
        return owner


def make_attributed_body(text: str) -> bytes:
    """A minimal typedstream blob the decoder reads as ``text``.

    Real blobs carry attribute dictionaries after the string; the decoder only
    needs the ``NSString`` marker, the 5-byte header, the length and the UTF-8
    payload, which is what this builds (lengths up to 65535 bytes).
    """
    payload = text.encode("utf-8")
    if len(payload) < 0x80:
        length = bytes([len(payload)])
    else:
        length = b"\x81" + len(payload).to_bytes(2, "little")
    return (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString"
        b"\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
        + length
        + payload
        + b"\x86\x84\x02iI\x01\x01\x92\x84\x84\x84\x0cNSDictionary\x00\x94\x84\x01i\x00\x86\x86"
    )
