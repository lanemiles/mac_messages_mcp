# Copyright (c) 2023 Carter Lasalle
"""Sending messages through Messages.app and verifying the outcome in chat.db."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from . import applescript as _applescript
from . import contacts as _contacts
from . import db as _db
from . import policy as _policy
from . import read as _read
from .db import APPLE_EPOCH_OFFSET
from .phone import canonical_handle, get_default_region, is_email_handle
from .untrusted import neutralize_untrusted_text


def send_message(
    recipient: str,
    message: str,
    *,
    group_chat: bool = False,
    attachment_paths: Sequence[str] | None = None,
) -> str:
    """Send a message using the Messages app with improved contact resolution.

    Args:
        recipient: Phone number, email, contact name, or special format for
            contact selection. Use "contact:N" to select the Nth contact from a
            previous ambiguous match. For group chats, use the chat ID from
            tool_get_chats (e.g., "chat123456789").
        message: Message text to send. May be empty when attachments are given.
        group_chat: Whether this is a group chat (uses chat ID instead of buddy)
        attachment_paths: Local files to send as attachments. Refused for group
            chats; a file transfer has no SMS/RCS fallback.

    Returns:
        Success or error message

    """
    # Convert to string to ensure phone numbers work properly
    recipient = str(recipient).strip()
    if _policy.read_only():
        return (
            f"Error: this server is read-only ({_policy.READ_ONLY_VAR}); "
            "nothing was sent."
        )

    resolved_attachments, attachment_error = _prepare_attachment_paths(
        attachment_paths,
    )
    if attachment_error is not None:
        return attachment_error
    if not message.strip() and not resolved_attachments:
        return "Error: provide message text or at least one attachment."
    if group_chat and resolved_attachments:
        return (
            "Error: attachments can only be sent to an individual recipient; "
            "Messages automation cannot attach files to a group chat."
        )

    # For group chats, skip contact lookup and use the chat ID directly
    if group_chat:
        # Use the recipient directly as the chat ID
        return _send_message_to_recipient(
            recipient,
            message,
            group_chat=True,
            attachment_paths=resolved_attachments,
        )

    # Handle contact selection format (contact:N)
    if recipient.lower().startswith("contact:"):
        try:
            # Get the selected index (1-based)
            index = int(recipient.split(":", 1)[1].strip()) - 1

            # Get the most recent contact matches from the shared store
            # (populated by tool_find_contact, send_message, get_recent_messages)
            recent_matches = _contacts.get_recent_contact_matches()
            if not recent_matches:
                return (
                    "No recent contact matches available. Please search for a "
                    "contact first."
                )

            if index < 0 or index >= len(recent_matches):
                return (
                    "Invalid selection. Please choose a number between 1 and "
                    f"{len(recent_matches)}."
                )

            # Get the selected contact
            contact = recent_matches[index]
            return _send_message_to_recipient(
                contact["phone"],
                message,
                contact["name"],
                group_chat=False,
                attachment_paths=resolved_attachments,
            )
        except (ValueError, IndexError) as e:
            return f"Error selecting contact: {e!s}"

    # Check if recipient is directly a phone number.
    if _contacts._looks_like_phone_input(recipient):
        formatted_number = _contacts._format_phone_for_messages(recipient)
        if not formatted_number:
            return (
                f"Error: '{recipient}' is not a usable phone number for region "
                f"{get_default_region()}. Use an E.164 number such as "
                "+14155551234, or set MAC_MESSAGES_REGION if your national "
                "numbers belong to another region."
            )
        return _send_message_to_recipient(
            formatted_number,
            message,
            group_chat=False,
            attachment_paths=resolved_attachments,
        )

    # Check if recipient is an email address
    if "@" in recipient:
        return _send_message_to_recipient(
            recipient,
            message,
            group_chat=False,
            attachment_paths=resolved_attachments,
        )

    # Try to find the contact by name
    contacts = _contacts.find_contact_by_name(recipient)

    if not contacts:
        return f"Error: Could not find any contact matching '{recipient}'"

    if len(contacts) == 1 or _contacts._single_person(contacts):
        # One person: use their phone number over an email handle.
        contact = _contacts._preferred_handle(contacts)
        return _send_message_to_recipient(
            contact["phone"],
            message,
            contact["name"],
            group_chat=False,
            attachment_paths=resolved_attachments,
        )
    # Store the matches for later selection (shared contact:N store)
    _contacts.set_recent_contact_matches(contacts)

    # Multiple matches, return them all
    contact_list = "\n".join(
        [
            f"{i + 1}. {neutralize_untrusted_text(c['name'])} "
            f"({neutralize_untrusted_text(c['phone'])})"
            for i, c in enumerate(contacts[:10])
        ],
    )
    return (
        f"Multiple contacts found matching "
        f"'{neutralize_untrusted_text(recipient)}'. Please specify which one "
        f"using 'contact:N' where N is the number:\n{contact_list}"
    )


def _row_text(row: dict[str, Any]) -> str | None:
    """Best-effort plain text for a message row (`text` column, else attributedBody).

    A decoded attributedBody has its attachment placeholders removed so it
    compares equal to the words Messages would have put in `text`.
    """
    text = row.get("text")
    if text:
        return text
    decoded = _read.extract_body_from_attributed(row.get("attributedBody"))
    if decoded is None or _read._is_attachment_placeholder_body(decoded):
        return decoded
    return _read._strip_attachment_placeholders(decoded)


def _verify_send_in_db(
    recipient: str,
    sent_after_unix: float,
    message_text: str | None = None,
    timeout_s: float = 6.0,
) -> dict[str, Any] | None:
    """Poll the Messages database for this call's outbound message.

    Looks for the message to `recipient` recorded after `sent_after_unix`.

    The AppleScript `send` command returns without error even when the
    message later fails (e.g. error 22 for an unroutable handle, or a
    recipient who deregistered from iMessage), so the AppleScript result
    alone cannot confirm a send. The authoritative outcome lives in
    chat.db's message.error / message.is_sent columns, which are populated
    within a second or two of the attempt.

    `sent_after_unix` alone is not a reliable correlation key: two sends to
    the same handle close together (e.g. rapid-fire tool calls) can both be
    "the latest row after time X" from either call's point of view, so a
    plain `ORDER BY date DESC LIMIT 1` can report the outcome of the wrong
    message. When `message_text` is given, matching rows are additionally
    filtered by exact body match (decoding attributedBody when `text` is
    NULL) so each call correlates to *its own* send rather than whichever
    row happens to be newest. If nothing matches the body (e.g. Messages
    re-encoded the text, or the row only has an attachment), we fall back to
    the plain latest-row behavior rather than reporting a false negative.

    Returns the row (guid, error, is_sent, is_delivered, service), or None
    if no row could be read before the timeout (e.g. no Full Disk Access).
    """
    apple_ns = int((sent_after_unix - APPLE_EPOCH_OFFSET - 1) * 1_000_000_000)

    def _matching_rows(handles: list[str]) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in handles)
        # m.error is aliased to send_error so a real row can't be mistaken
        # for query_messages_db's {"error": ...} failure dict.
        query = f"""
            SELECT m.guid, m.error AS send_error, m.is_sent, m.is_delivered,
                   m.service, m.text, m.attributedBody
            FROM message m
            JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.is_from_me = 1
              AND h.id IN ({placeholders})
              AND m.date >= ?
            ORDER BY m.date DESC
            LIMIT 10
        """
        results = _db.query_messages_db(query, (*tuple(handles), apple_ns))
        if results and "guid" in results[0]:
            return results
        return []

    def _best_row(handles: list[str]) -> dict[str, Any] | None:
        rows = _matching_rows(handles)
        if not rows:
            return None
        if message_text is not None:
            for row in rows:
                if _row_text(row) == message_text:
                    return row
            # No row's body matched this send -- fall back to the newest row
            # rather than reporting nothing (e.g. attachment-only messages,
            # or Messages normalizing the text on write).
        return rows[0]

    exact = [recipient.strip()]
    variants = _contacts._candidate_handles(recipient)
    deadline = time.time() + timeout_s
    row = None
    while time.time() < deadline:
        # Prefer the exact handle the send targeted; only widen to format
        # variants when the exact handle has no row (Messages sometimes
        # records the handle in a different format than it was given).
        row = _best_row(exact) or _best_row(variants)
        # Stop polling once the row resolves: an error code appears, or
        # the message is marked sent. Otherwise keep waiting.
        if row is not None and (row.get("send_error") or row.get("is_sent")):
            return row
        time.sleep(0.5)
    return row


def _report_send_outcome(
    recipient: str,
    display_name: str,
    service: str,
    sent_after_unix: float,
    message_text: str | None = None,
) -> str:
    """Render the database verification result as the string returned to the caller.

    Never claims success for a message the database says failed.
    """
    display_name = neutralize_untrusted_text(display_name)
    row = _verify_send_in_db(recipient, sent_after_unix, message_text)
    if row is None:
        # Could not read the database (or the row never appeared); report
        # honestly instead of claiming success.
        return (
            f"Message to {display_name} was handed to Messages.app via {service}, "
            f"but delivery could not be verified in the Messages database."
        )
    actual_service = row.get("service") or service
    if row.get("send_error"):
        return (
            f"Error: message to {display_name} failed to send "
            f"(Messages error code {row['send_error']}, service {actual_service}). "
            f"The recipient may not be reachable via {actual_service}."
        )
    status = "delivered" if row.get("is_delivered") else "sent"
    return f"Message {status} successfully via {actual_service} to {display_name}"


def _write_message_file(message: str) -> str:
    """Write the message body to an owner-only temp file and return its path."""
    # Owner-only temp file (mkstemp is 0o600). NamedTemporaryFile is
    # world-readable on some systems and is flagged as CWE-377.
    fd, file_path = tempfile.mkstemp(prefix="mac-messages-", suffix=".txt")
    try:
        os.write(fd, message.encode("utf-8"))
    finally:
        os.close(fd)
    return file_path


def _send_via_temp_file(
    recipient: str,
    message: str,
    file_path: str,
    contact_name: str | None,
    *,
    group_chat: bool,
) -> str:
    """Run the file-based AppleScript send and report the database outcome."""
    safe_recipient = _applescript.escape_applescript(recipient)
    safe_file_path = _applescript.escape_applescript(file_path)

    # Adjust the AppleScript command based on whether this is a group chat
    if not group_chat:
        command = (
            'tell application "Messages" to send (read (POSIX file '
            f'"{safe_file_path}") as «class utf8») to participant '
            f'"{safe_recipient}" of (1st service whose service type = iMessage)'
        )
    else:
        # Group chats are addressed by their full chat id.
        # The Messages dictionary requires `chat id "…"`, NOT `chat "…"`:
        # the latter looks up by the chat's display name and fails for guid-style
        # identifiers (raises -1728 "Can't get chat …").
        command = (
            'tell application "Messages" to send (read (POSIX file '
            f'"{safe_file_path}") as «class utf8») to chat id "{safe_recipient}"'
        )

    sent_at = time.time()

    # Run the AppleScript
    result = _applescript.run_applescript(command)

    # Check result
    if result.startswith("Error:"):
        # Try fallback to direct method
        return _send_message_direct(
            recipient,
            message,
            contact_name,
            group_chat=group_chat,
        )

    # AppleScript accepted the send; confirm the outcome in the database
    display_name = neutralize_untrusted_text(
        contact_name or recipient,
    )
    if group_chat:
        # Group chat ids can't be verified against a single handle
        return f"Message sent successfully to {display_name}"
    return _report_send_outcome(
        recipient,
        display_name,
        "iMessage",
        sent_at,
        message,
    )


def _prepare_attachment_paths(
    attachment_paths: Sequence[str] | None,
) -> tuple[list[str], str | None]:
    """Validate and resolve local files to send as attachments.

    Returns ``(paths, error)``. Every path is checked before the first byte is
    sent, so an invalid path cannot leave a partially-delivered message behind.
    """
    if not attachment_paths:
        return [], None
    resolved: list[str] = []
    for raw in attachment_paths:
        path = Path(str(raw)).expanduser()
        try:
            resolved_path = path.resolve(strict=True)
        except OSError:
            return [], f"Error: attachment not found: {raw}"
        if not resolved_path.is_file():
            return [], f"Error: attachment is not a regular file: {raw}"
        policy_error = _policy.attachment_path_error(resolved_path)
        if policy_error is not None:
            return [], policy_error
        resolved.append(str(resolved_path))
    return resolved, None


def _send_attachment_file(recipient: str, file_path: str) -> str:
    """Hand one local file to Messages.app over iMessage.

    A file transfer has no SMS/RCS path, so this never falls back: an
    unreachable iMessage recipient fails rather than silently dropping the file.
    """
    safe_recipient = _applescript.escape_applescript(recipient)
    safe_path = _applescript.escape_applescript(file_path)
    command = (
        'tell application "Messages" to send (POSIX file '
        f'"{safe_path}") to participant "{safe_recipient}" of '
        "(1st service whose service type = iMessage)"
    )
    return _applescript.run_applescript(command)


def _send_message_to_recipient(
    recipient: str,
    message: str,
    contact_name: str | None = None,
    *,
    group_chat: bool = False,
    attachment_paths: Sequence[str] = (),
) -> str:
    """Send a message to a specific recipient using a file-based approach.

    Args:
        recipient: Phone number or email
        message: Message text to send
        contact_name: Optional contact name for the success message
        group_chat: Whether this is a group chat
        attachment_paths: Resolved local files to send first (direct
            recipients only).

    Returns:
        Success or error message

    """
    if group_chat and attachment_paths:
        return (
            "Error: attachments can only be sent to an individual recipient; "
            "Messages automation cannot attach files to a group chat."
        )

    # Every send, however it was addressed, passes through here: enforce the
    # server-side policy on the resolved recipient before anything is sent.
    if _policy.read_only():
        return (
            f"Error: this server is read-only ({_policy.READ_ONLY_VAR}); "
            "nothing was sent."
        )
    recipient_error = _policy.recipient_error(recipient)
    if recipient_error is not None:
        return recipient_error
    budget_error = _policy.send_budget_error()
    if budget_error is not None:
        return budget_error

    attachment_notes: list[str] = []
    for path in attachment_paths:
        result = _send_attachment_file(recipient, path)
        label = neutralize_untrusted_text(Path(path).name)
        if result.startswith("Error:"):
            attachment_notes.append(f"{label} failed ({result[6:].strip()})")
        else:
            attachment_notes.append(f"{label} sent")

    if not message.strip():
        target = neutralize_untrusted_text(contact_name or recipient)
        if not attachment_notes:
            return "Error: provide message text or at least one attachment."
        return (
            f"Sent {len(attachment_notes)} attachment(s) via iMessage to "
            f"{target}: " + "; ".join(attachment_notes)
        )

    file_path: str | None = None
    try:
        file_path = _write_message_file(message)
        result = _send_via_temp_file(
            recipient,
            message,
            file_path,
            contact_name,
            group_chat=group_chat,
        )
    except (OSError, sqlite3.Error, subprocess.SubprocessError):
        # Try fallback method
        result = _send_message_direct(
            recipient,
            message,
            contact_name,
            group_chat=group_chat,
        )
    finally:
        # Clean up the temporary file
        if file_path:
            with suppress(OSError):
                Path(file_path).unlink()

    if attachment_notes:
        result += " Attachments: " + "; ".join(attachment_notes)
    return result


def _check_imessage_availability(recipient: str) -> bool:
    """Check if recipient has iMessage available by querying the messages database.

    Args:
        recipient: Phone number or email to check

    Returns:
        True if iMessage is available, False otherwise

    """
    if is_email_handle(recipient):
        # handle.id has no declared collation, so it compares case-sensitively
        # while the canonical form is lowercased. Fold both sides instead of
        # only the input, or an address stored in mixed case stops matching the
        # mixed-case spelling that used to find it.
        query_params = (canonical_handle(recipient) or recipient.strip(),)
        where_clause = "h.id = ? COLLATE NOCASE"
    else:
        # Resolve through the same canonical matching the message lookup uses,
        # so a number that has iMessage history is not reported as SMS-only
        # just because it is written differently from the stored handle.
        handle_rowids = _contacts.find_handles_by_phone(recipient)

        if not handle_rowids:
            return False

        query_params = tuple(handle_rowids)
        placeholders = ", ".join(["?" for _ in query_params])
        where_clause = f"h.ROWID IN ({placeholders})"

    query = f"""
        SELECT
            h.ROWID,
            h.service,
            COUNT(m.guid) as text_count,
            COUNT(CASE WHEN m.error != 0 then 1 END) as errors
        FROM handle h
        LEFT JOIN message m ON h.ROWID = m.handle_id
        WHERE {where_clause}
        GROUP BY
            h.ROWID,
            h.service
        """

    result = _db.query_messages_db(query, query_params)

    if not result or "error" in result[0]:
        return False

    for row in result:
        service_type = row.get("service", "")
        text_count = row.get("text_count", 0)
        num_errors = row.get("errors", 0)
        # Only iMessage counts when some messages went through without errors.
        if num_errors < text_count and service_type in {"iMessage", "iMessageLite"}:
            return True

    return False


def _send_message_sms(
    recipient: str,
    message: str,
    contact_name: str | None = None,
) -> str:
    """Send message via SMS/RCS using AppleScript.

    Args:
        recipient: Phone number to send to
        message: Message content
        contact_name: Optional contact name for display

    Returns:
        Success or error message

    """
    safe_message = _applescript.escape_applescript(message)
    safe_recipient = _applescript.escape_applescript(recipient)

    script = f"""
    tell application "Messages"
        try
            -- Try to find SMS service
            set smsService to first account whose service type = SMS and enabled is true

            -- Send message via SMS
            send "{safe_message}" to participant "{safe_recipient}" of smsService

            -- Wait briefly to check for immediate errors
            delay 1

            return "success"
        on error errMsg
            return "error:" & errMsg
        end try
    end tell
    """

    try:
        sent_at = time.time()
        result = _applescript.run_applescript(script)
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as e:
        return f"Error sending SMS: {e!s}"
    if result.startswith("error:"):
        return f"Error sending SMS: {result[6:]}"
    if result.strip() == "success":
        display_name = contact_name or recipient
        return _report_send_outcome(
            recipient,
            display_name,
            "SMS",
            sent_at,
            message,
        )
    return f"Unknown SMS result: {result}"


def _digit_check_lines(safe_recipient: str) -> str:
    """Build AppleScript lines setting digitFound when the recipient has a digit."""
    contains = [f'"{safe_recipient}" contains "{digit}"' for digit in range(10)]
    return f"set digitFound to ({' or '.join(contains)})"


def _send_message_direct(
    recipient: str,
    message: str,
    contact_name: str | None = None,
    *,
    group_chat: bool = False,
) -> str:
    """Enhanced direct AppleScript method for sending messages with SMS/RCS fallback.

    This function implements automatic fallback from iMessage to SMS/RCS when:
    1. Recipient doesn't have iMessage
    2. iMessage delivery fails
    3. iMessage service is unavailable

    Args:
        recipient: Phone number or email
        message: Message content
        contact_name: Optional contact name for display
        group_chat: Whether this is a group chat

    Returns:
        Success or error message with service type used

    """
    # Clean the inputs for AppleScript using the central helper, which also
    # handles newlines, tabs, and Unicode line/paragraph separators.
    safe_message = _applescript.escape_applescript(message)
    safe_recipient = _applescript.escape_applescript(recipient)
    digit_check = _digit_check_lines(safe_recipient)

    # For group chats, stick to iMessage only (SMS doesn't support group chats)
    if group_chat:
        script = f"""
        tell application "Messages"
            try
                -- Try to get the existing chat by its full id.
                -- `chat id "…"` looks up by guid; plain `chat "…"` looks up
                -- name and fails on guid-style identifiers with -1728 "Can't get chat".
                set targetChat to chat id "{safe_recipient}"

                -- Send the message
                send "{safe_message}" to targetChat

                -- Wait briefly to check for immediate errors
                delay 1

                -- Return success
                return "success"
            on error errMsg
                -- Chat method failed
                return "error:" & errMsg
            end try
        end tell
        """

        try:
            result = _applescript.run_applescript(script)
        except (OSError, subprocess.SubprocessError) as e:
            return f"Error sending group message: {e!s}"
        if result.startswith("error:"):
            return f"Error sending group message: {result[6:]}"
        if result.strip() == "success":
            display_name = neutralize_untrusted_text(
                contact_name or recipient,
            )
            return f"Group message sent successfully to {display_name}"
        return f"Unknown group message result: {result}"
    # For individual messages, try iMessage first with automatic SMS fallback
    # Enhanced AppleScript with built-in fallback logic
    script = f"""
    tell application "Messages"
        try
            -- First, try iMessage
            set targetService to 1st service whose service type = iMessage

            try
                -- Try to get the existing participant if possible
                set targetBuddy to participant "{safe_recipient}" of targetService

                -- Send the message via iMessage
                send "{safe_message}" to targetBuddy

                -- Wait briefly to check for immediate errors
                delay 2

                -- Return success with service type
                return "success:iMessage"
            on error iMessageErr
                -- iMessage failed, try SMS fallback if recipient looks like
                -- a phone number
                try
                    -- Check if recipient has any digit in it
                    {digit_check}
                    -- Try SMS service
                    if digitFound then
                        set smsService to first account whose service type = SMS
                        send "{safe_message}" to participant
                            "{safe_recipient}" of smsService

                        -- Wait briefly to check for immediate errors
                        delay 2

                        return "success:SMS"
                    else
                        -- Not a phone number, can't use SMS
                        return "error:iMessage failed and SMS unavailable - "
                            & iMessageErr
                    end if
                on error smsErr
                    -- Both iMessage and SMS failed
                    return "error:Both iMessage and SMS failed - iMessage: "
                        & iMessageErr & " SMS: " & smsErr
                end try
            end try
        on error generalErr
            return "error:" & generalErr
        end try
    end tell
    """
    try:
        sent_at = time.time()
        result = _applescript.run_applescript(script)
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as e:
        return f"Error sending message: {e!s}"

    display_name = contact_name or recipient

    if result.startswith("error:"):
        return f"Error sending message: {result[6:]}"
    status = result.strip()
    if status in {"success:iMessage", "success"}:
        return _report_send_outcome(
            recipient,
            display_name,
            "iMessage",
            sent_at,
            message,
        )
    if status == "success:SMS":
        return _report_send_outcome(
            recipient,
            display_name,
            "SMS",
            sent_at,
            message,
        )
    return f"Unknown result: {result}"
