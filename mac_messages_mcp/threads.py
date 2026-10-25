# Copyright (c) 2023 Carter Lasalle
"""Thread-first reading: one conversation, in order, paged by message cursors.

Pages are ordered by ``message.date`` (ROWID breaks ties), never by ROWID
alone: on a Mac whose history came down from Messages in iCloud, ROWID is the
order rows were *downloaded*, so a 2018 text can outrank yesterday's. The
cursors are still message ROWIDs (the ``#N`` on each line); the server looks
up that row's date and pages relative to it.
"""

from __future__ import annotations

from typing import Any

from . import chats as _chats
from . import db as _db
from . import output as _output
from . import read as _read
from .output import _MAX_MESSAGE_LIMIT
from .untrusted import bound_untrusted_output, neutralize_untrusted_text


@bound_untrusted_output
def get_conversation(
    chat_id: str,
    *,
    limit: int = 50,
    before_rowid: int | None = None,
    after_rowid: int | None = None,
    format: str = "text",
) -> str:
    """Read one chat's messages in chronological order with paging cursors.

    With no cursor, the newest ``limit`` messages are returned oldest-first.
    ``before_rowid`` pages back to older messages; ``after_rowid`` pages forward.
    The header names the chat, its kind and its participants; the footer (or
    the JSON ``cursors`` object) gives the ROWIDs to pass next.

    Args:
        chat_id: Chat identifier from tool_get_chats / tool_list_conversations,
            in either spelling (``chat123`` or ``iMessage;+;chat123``).
        limit: Messages per page (default 50, max 1000).
        before_rowid: Return only messages with a smaller ROWID (older).
        after_rowid: Return only messages with a greater ROWID (newer).
        format: ``"text"`` or ``"json"``.

    """
    format_error = _output._check_format(format)
    if format_error is not None:
        return format_error
    if limit <= 0:
        return "Error: limit must be positive."
    if limit > _MAX_MESSAGE_LIMIT:
        return f"Error: limit must be at most {_MAX_MESSAGE_LIMIT}."
    if before_rowid is not None and after_rowid is not None:
        return "Error: pass before_rowid or after_rowid, not both."
    as_json = format == "json"

    chat = _chats._find_chat_by_identifier(chat_id)
    if chat is None:
        return (
            f"No chat found with chat_id '{chat_id}'. Use tool_get_chats or "
            "tool_list_conversations to find one."
        )
    chat_row_id = chat["ROWID"]
    is_group = chat.get("style") == 43

    clauses = ["cmj.chat_id = ?"]
    params: list[Any] = [chat_row_id]
    cursor = before_rowid if before_rowid is not None else after_rowid
    if cursor is not None:
        anchor = _db.query_messages_db(
            "SELECT date, ROWID FROM message WHERE ROWID = ?", (cursor,)
        )
        if anchor and "error" in anchor[0]:
            if as_json:
                return _output._json_dumps(
                    {"error": _output._json_text(anchor[0]["error"])}
                )
            return f"Error accessing messages: {anchor[0]['error']}"
        if not anchor:
            return (
                f"Error: no message with ROWID {cursor}; cursors come from a "
                "previous page of this conversation."
            )
        # Chronological position of the anchor row: its date, then ROWID so
        # two rows with the same timestamp still have a fixed order.
        comparison = "<" if before_rowid is not None else ">"
        clauses.append(
            f"(m.date {comparison} ? OR (m.date = ? AND m.ROWID {comparison} ?))"
        )
        anchor_date = anchor[0].get("date")
        params.extend([anchor_date, anchor_date, cursor])
    # Forward paging reads ascending; the default and backward paging read the
    # newest rows descending and are flipped into chronological order below.
    order = "ASC" if after_rowid is not None else "DESC"
    # One extra row tells us whether another page exists in that direction.
    params.append(limit + 1)
    query = f"""
    SELECT
        {_read._message_columns()}
    FROM message m
    JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
    WHERE {" AND ".join(clauses)}
    ORDER BY m.date {order}, m.ROWID {order}
    LIMIT ?
    """
    rows = _db.query_messages_db(query, tuple(params))
    if rows and "error" in rows[0]:
        if as_json:
            return _output._json_dumps({"error": _output._json_text(rows[0]["error"])})
        return f"Error accessing messages: {rows[0]['error']}"
    has_more = len(rows) > limit
    rows = rows[:limit]
    if order == "DESC":
        rows.reverse()

    participants = _chats._participants_for_chats([chat_row_id]).get(chat_row_id, [])
    chat_mapping = _chats.get_chat_mapping()
    ids = [r["ROWID"] for r in rows if r.get("ROWID") is not None]
    attachments_by_msg = _read._attachments._attachments_for_message_ids(ids)
    records = _read._build_message_records(
        rows,
        chat_mapping=chat_mapping,
        attachments_by_msg=attachments_by_msg,
        chat_display_name=chat.get("display_name"),
        chat_is_group=is_group,
    )
    # ``rows`` is chronological here, so the boundary cursors are the first
    # and last rows shown — not min/max ROWID, which need not be chronological.
    oldest = ids[0] if ids else None
    newest = ids[-1] if ids else None
    # Older pages exist below the oldest row shown unless this page reached
    # the start; newer pages exist above the newest row when paging back.
    has_older = has_more if order == "DESC" else (before_rowid is not None or bool(ids))
    if order == "ASC":
        has_older = True  # we are strictly after a cursor, so older rows exist
    has_newer = (
        has_more if order == "ASC" else after_rowid is None and before_rowid is not None
    )

    chat_json = _chats._chat_json(
        {
            "chat_identifier": chat.get("chat_identifier"),
            "display_name": chat.get("display_name"),
            "style": chat.get("style"),
        },
        participants,
    )
    if as_json:
        return _output._json_dumps(
            {
                "chat": chat_json,
                "messages": [_read._message_json(r) for r in records],
                "cursors": {
                    "before_rowid": (
                        oldest if has_older and oldest is not None else None
                    ),
                    "after_rowid": newest,
                    "has_older": bool(has_older and oldest is not None),
                },
            }
        )

    kind = _chats._conversation_kind(chat.get("style"), chat.get("chat_identifier"))
    label = chat.get("display_name") or chat.get("chat_identifier") or chat_id
    names = ", ".join(neutralize_untrusted_text(p["name"]) for p in participants)
    header = (
        f"{neutralize_untrusted_text(label)} (chat ID: "
        f"{neutralize_untrusted_text(chat.get('chat_identifier'))}) [{kind}]"
    )
    if names:
        header += f", {len(participants)} participant(s): {names}"
    lines = [header]
    if not records:
        lines.append("(no messages in this range)")
    lines.extend(_read._render_message_line(r) for r in records)
    cursors = []
    if has_older and oldest is not None:
        cursors.append(f"older: before_rowid={oldest}")
    if newest is not None:
        cursors.append(f"newer: after_rowid={newest}")
    if cursors:
        lines.append("(" + " | ".join(cursors) + ")")
    return "\n".join(lines)
