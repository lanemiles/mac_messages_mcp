# Copyright (c) 2023 Carter Lasalle
"""Resolve a read's ``contact`` / ``chat_id`` filter into a message-table scope.

Every read tool accepts the same two filters. ``resolve_scope`` turns them
into a :class:`Scope`: the handle ROWIDs to match, or the chat to match, or an
error message for the caller to return. The resolution steps are small
functions, in the order the old monolithic resolver applied them: the
``contact:N`` selector, then a name (AddressBook fuzzy match, falling back to a
chat display name), then a bare phone number or email address.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from . import chats as _chats
from . import contacts as _contacts
from . import db as _db
from .phone import canonical_handle, digits_only, is_email_handle


@dataclass
class Scope:
    """What a filtered read should match.

    At most one of ``handle_ids`` / ``chat_row_id`` is set; ``error`` is a
    user-facing message when the filter could not be resolved. Unpacks as the
    historical five-tuple for callers written against that shape.
    """

    handle_ids: list[int] | None = None
    chat_row_id: int | None = None
    chat_display_name: str | None = None
    chat_is_group: bool = False
    error: str | None = None
    # Group chats carry their participants here when resolved by chat.
    extra: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __iter__(self) -> Iterator[Any]:
        yield self.handle_ids
        yield self.chat_row_id
        yield self.chat_display_name
        yield self.chat_is_group
        yield self.error

    @classmethod
    def failed(cls, message: str) -> Scope:
        return cls(error=message)

    def sql_clauses(self, alias: str = "m") -> tuple[list[str], list[Any]]:
        """WHERE fragments and parameters restricting ``alias`` to this scope."""
        clauses: list[str] = []
        params: list[Any] = []
        if self.handle_ids:
            placeholders = ", ".join("?" for _ in self.handle_ids)
            clauses.append(f"{alias}.handle_id IN ({placeholders})")
            params.extend(self.handle_ids)
        if self.chat_row_id is not None:
            clauses.append(
                f"{alias}.ROWID IN (SELECT message_id FROM chat_message_join "
                "WHERE chat_id = ?)"
            )
            params.append(self.chat_row_id)
        return clauses, params


def resolve_scope(contact: str | None, chat_id: str | None) -> Scope:
    """Resolve an optional contact/chat filter into a :class:`Scope`.

    Shared by every read so they all accept the same filters, including the
    ``contact:N`` selector printed by ``tool_find_contact``.
    """
    if contact and chat_id:
        return Scope.failed("Error: Provide either contact or chat_id, not both.")
    if chat_id:
        return _scope_for_chat_id(str(chat_id).strip())
    if contact:
        return _scope_for_contact(str(contact).strip())
    return Scope()


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _scope_for_chat_id(chat_id: str) -> Scope:
    if not chat_id:
        return Scope.failed("Error: chat_id cannot be empty.")
    chat = _chats._find_chat_by_identifier(chat_id)
    if not chat:
        return Scope.failed(
            f"No group chat found with chat_id '{chat_id}'. "
            "Use tool_get_chats to list available group chats."
        )
    return _scope_for_chat_row(chat, fallback_name=chat_id)


def _scope_for_chat_row(chat: dict[str, Any], *, fallback_name: str) -> Scope:
    # Only group chats (style 43) get the "[Name]" prefix; 1:1 and business
    # chats (style 45) would misattribute the thread.
    return Scope(
        chat_row_id=chat["ROWID"],
        chat_display_name=chat.get("display_name") or fallback_name,
        chat_is_group=chat.get("style") == 43,
    )


def _looks_like_address(contact: str) -> bool:
    """A phone number or email, as opposed to a name to fuzzy-match."""
    return is_email_handle(contact) or all(
        c.isdigit() or c in "+- ()@." for c in contact
    )


def _scope_for_contact(contact: str) -> Scope:
    if contact.lower().startswith("contact:"):
        selected = _resolve_contact_selector(contact)
        if isinstance(selected, Scope):
            return selected
        contact = selected

    handle_ids: list[int] | None = None
    if not _looks_like_address(contact):
        outcome = _scope_for_name(contact)
        if isinstance(outcome, Scope):
            return outcome
        contact, handle_ids = outcome

    if not handle_ids:
        handle_ids = _contacts._handles_for_address(contact)
    if handle_ids:
        return Scope(handle_ids=handle_ids)
    return _no_history_error(contact)


def _resolve_contact_selector(contact: str) -> str | Scope:
    """``contact:N`` -> the Nth address from the last match list, or an error."""
    parts = contact.split(":", 1)
    if len(parts) < 2 or not parts[1].strip():
        return Scope.failed(
            "Error: Invalid contact selection format. Use 'contact:N' "
            "where N is a positive number."
        )
    try:
        index = int(parts[1].strip()) - 1
    except ValueError:
        return Scope.failed(
            "Error: Contact selection must be a number. Use 'contact:N' "
            "where N is a positive number."
        )
    if index < 0:
        return Scope.failed(
            "Error: Contact selection must be a positive number (starting from 1)."
        )
    recent = _contacts.get_recent_contact_matches()
    if not recent:
        return Scope.failed(
            "No recent contact matches available. Please search for a contact first."
        )
    if index >= len(recent):
        return Scope.failed(
            f"Invalid selection. Please choose a number between 1 and {len(recent)}."
        )
    return str(recent[index]["phone"])


def _scope_for_name(contact: str) -> Scope | tuple[str, list[int] | None]:
    """Fuzzy-match a name; returns a finished Scope or (address, handle_ids)."""
    matches = _contacts.find_contact_by_name(contact)

    # An exact chat display-name match outranks weak AddressBook noise:
    # genuine name hits score 0.80+ (exact token 0.95, prefix 0.80+), so
    # AddressBook hits all near the 0.60 floor alongside an exact chat match
    # (e.g. "Poke" vs two unrelated humans) mean the chat.
    chat_match = None
    if not matches or _contacts._all_weak_matches(matches):
        chat_match = _chats._find_chat_by_display_name(contact)

    if chat_match is not None and not isinstance(chat_match, list):
        return _scope_for_chat_row(chat_match, fallback_name=contact)
    if chat_match is not None and not matches:
        chat_list = "\n".join(
            f"{i + 1}. {c['display_name']} (chat ID: {c['chat_identifier']})"
            for i, c in enumerate(chat_match)
        )
        return Scope.failed(
            f"Multiple chats found matching '{contact}'. Please specify "
            f"which one using 'chat_id' from tool_get_chats:\n{chat_list}"
        )
    if not matches:
        return Scope.failed(f"No contacts found matching '{contact}'.")
    if len(matches) == 1:
        return str(matches[0]["phone"]), None
    if _contacts._single_person(matches):
        # One person, several handles (phone + iMessage emails): read across
        # all of them rather than asking which to pick.
        handle_ids: list[int] = []
        for m in matches:
            handle_ids.extend(_contacts._handles_for_address(str(m["phone"])))
        return (
            str(_contacts._preferred_handle(matches)["phone"]),
            list(dict.fromkeys(handle_ids)),
        )
    # Several people: store the list so a follow-up contact:N resolves.
    _contacts.set_recent_contact_matches(matches)
    contact_list = "\n".join(
        f"{i + 1}. {c['name']} ({c['phone']})" for i, c in enumerate(matches[:10])
    )
    return Scope.failed(
        f"Multiple contacts found matching '{contact}'. Please specify which "
        f"one using 'contact:N' where N is the number:\n{contact_list}"
    )


def _no_history_error(contact: str) -> Scope:
    """Distinguish 'no messages with this address' from 'no such handle'.

    Matches on the digits of the canonical number so a national-format input
    still finds a handle stored internationally.
    """
    normalized = digits_only(canonical_handle(contact) or contact)
    results = _db.query_messages_db(
        "SELECT COUNT(*) as count FROM message m "
        "JOIN handle h ON m.handle_id = h.ROWID WHERE h.id LIKE ?",
        (f"%{normalized}%",),
    )
    if results and "error" not in results[0] and results[0].get("count", 0) == 0:
        return Scope.failed(f"No message history found with '{contact}'.")
    return Scope.failed(
        f"Could not find any messages with contact '{contact}'. "
        "Verify the phone number or email is correct."
    )
