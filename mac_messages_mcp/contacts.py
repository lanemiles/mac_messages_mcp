# Copyright (c) 2023 Carter Lasalle
"""Contacts and handles: AddressBook lookup, fuzzy name matching, the contact:N
selection store, handle resolution, and Contacts.app writes."""

from __future__ import annotations

import difflib
import logging
import operator
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from . import applescript as _applescript
from . import db as _db
from . import policy as _policy
from .db import _FULL_DISK_ACCESS_HINT
from .phone import (
    canonical_handle,
    contact_key,
    digits_only,
    handle_variants,
    lookup_keys,
    to_dialable_e164,
)
from .untrusted import neutralize_untrusted_text

logger = logging.getLogger(__name__)


def normalize_phone_number(phone: str) -> str:
    """Normalize a phone number by removing all non-digit characters."""
    return digits_only(phone)


def _format_phone_for_messages(phone: str) -> str:
    """Return the phone number format Messages resolves most reliably.

    National-format numbers are expanded using the region this Mac is
    configured for, so ``06 39 98 00 01`` becomes ``+33639980001`` in France
    and ``(555) 555-0142`` becomes ``+15555550142`` in the United States.
    Returns an empty string when the input is not a usable phone number.

    Numbers that are only dialable from inside their own local area, a
    seven-digit NANP local among them, are refused rather than expanded, so a
    half-typed number is reported back to the caller instead of being handed
    to Messages.app. That test comes from the numbering plan, not from a digit
    count, which is why an eight-digit Norwegian number is accepted.
    """
    return to_dialable_e164(phone) or ""


def _looks_like_phone_input(value: str) -> bool:
    """Return True when the input is a phone number, not a contact name.

    Accepts the separators people actually type, including the dots used in
    French national notation (``05.39.98.00.03``).
    """
    return bool(value) and all(c.isdigit() or c in "+-. ()" for c in value)


# Global cache for contacts map
_CONTACTS_CACHE = None


_LAST_CACHE_UPDATE = 0


_CACHE_TTL_S = 300  # 5 minutes in seconds


# Inclusive (start, end) code-point ranges for characters stripped as emoji.
# Kept as ordinal comparisons so CodeQL py/overly-large-range does not treat
# supplementary-plane regex ranges as overlapping U+FFFD, and so the old
# catch-all U+24C2-U+1F251 span cannot swallow CJK and other non-emoji text.
_EMOJI_ORDINAL_RANGES = (
    (0x1F600, 0x1F64F),  # emoticons
    (0x1F300, 0x1F5FF),  # symbols & pictographs
    (0x1F680, 0x1F6FF),  # transport & map symbols
    (0x1F700, 0x1F77F),  # alchemical symbols
    (0x1F780, 0x1F7FF),  # geometric shapes extended
    (0x1F800, 0x1F8FF),  # supplemental arrows-C
    (0x1F900, 0x1F9FF),  # supplemental symbols and pictographs
    (0x1FA00, 0x1FA6F),  # chess symbols
    (0x1FA70, 0x1FAFF),  # symbols and pictographs extended-A
    (0x2702, 0x27B0),  # dingbats
    (0x2600, 0x26FF),  # miscellaneous symbols (☀, ⚡, ♥, …)
    (0x24C2, 0x24C2),  # circled M
    (0x1F170, 0x1F251),  # enclosed alphanumeric supplement through 🉑
)


def _is_emoji_codepoint(codepoint: int) -> bool:
    return any(start <= codepoint <= end for start, end in _EMOJI_ORDINAL_RANGES)


def _strip_emoji(text: str) -> str:
    return "".join(ch for ch in text if not _is_emoji_codepoint(ord(ch)))


def _clean_text(text: str, *, strip_punctuation: bool = False) -> str:
    """Remove emoji and normalise whitespace.

    Args:
        text: The string to clean.
        strip_punctuation: If True, also remove all characters that are not
            alphanumeric, whitespace, apostrophes, or hyphens (used for
            contact-name matching).

    """
    text = _strip_emoji(text)
    if strip_punctuation:
        text = re.sub(r"[^\w\s\'\-]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def clean_name(name: str) -> str:
    """Clean a name by removing emojis, punctuation, and extra whitespace."""
    return _clean_text(name, strip_punctuation=True)


def fuzzy_match(
    query: str,
    candidates: list[tuple[str, Any]],
    threshold: float = 0.6,
) -> list[tuple[str, Any, float]]:
    """Find fuzzy matches between query and a list of candidates.

    Matches use token-based scoring to properly handle first name searches:
    - Exact token match (e.g., "alex" matches first name "Alex") scores 0.95
    - Query as prefix of token scores 0.85
    - Token as prefix of query scores 0.80
    - Fuzzy match on individual tokens uses best token score

    Args:
        query: The search string
        candidates: List of (name, value) tuples to search through
        threshold: Minimum similarity score (0-1) to consider a match

    Returns:
        List of (name, value, score) tuples for matches, sorted by score

    """
    query = clean_name(query).lower()
    if not query:
        return []

    results: list[tuple[str, Any, float]] = []

    for name, value in candidates:
        clean_candidate = clean_name(name).lower()

        # Try exact full match first (case insensitive)
        if query == clean_candidate:
            results.append((name, value, 1.0))
            continue

        # Token-based matching: split candidate into words/tokens
        tokens = clean_candidate.split()
        best_token_score = 0.0

        for token in tokens:
            # Exact token match (e.g., query "alex" matches token "alex")
            if query == token:
                best_token_score = max(best_token_score, 0.95)
            # Query is prefix of token (e.g., "ale" matches "alex")
            elif token.startswith(query):
                # Score based on how much of the token is matched
                prefix_score = 0.85 * (len(query) / len(token))
                best_token_score = max(best_token_score, prefix_score)
            # Token is prefix of query (e.g., "alex" when searching "alexis")
            elif query.startswith(token):
                prefix_score = 0.80 * (len(token) / len(query))
                best_token_score = max(best_token_score, prefix_score)
            else:
                # Fuzzy match on individual token
                token_score = difflib.SequenceMatcher(None, query, token).ratio()
                best_token_score = max(best_token_score, token_score)

        # Also try matching query against full name for multi-word queries
        if " " in query or best_token_score < threshold:
            full_score = difflib.SequenceMatcher(None, query, clean_candidate).ratio()
            best_token_score = max(best_token_score, full_score)

        if best_token_score >= threshold:
            results.append((name, value, best_token_score))

    # Sort results by score (highest first)
    return sorted(results, key=operator.itemgetter(2), reverse=True)


def _query_one_addressbook_db(
    db_path: str,
    query: str,
    params: tuple,
) -> list[dict[str, Any]] | None:
    """Run query against one AddressBook copy, or None when it is unreadable."""
    try:
        conn = _db._connect_sqlite_readonly(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(query, params)
        results = [dict(row) for row in cursor.fetchall()]
    except (sqlite3.Error, ValueError, TypeError) as e:
        # If we can't access this one, try the next database
        logger.warning("Cannot access %s: %s", db_path, e)
        return None
    conn.close()
    return results


def _addressbook_db_paths() -> tuple[list[str], str]:
    """Return AddressBook database paths plus the sources glob for messages.

    Checks both the top-level DB and source-specific DBs (iCloud, Google,
    Exchange, etc.).
    """
    home_dir = Path("~").expanduser()
    sources_dir = home_dir / "Library/Application Support/AddressBook/Sources"
    toplevel_path = (
        home_dir / "Library/Application Support/AddressBook/AddressBook-v22.abcddb"
    )
    db_paths = [str(path) for path in sources_dir.glob("*/AddressBook-v22.abcddb")]
    if toplevel_path.exists():
        db_paths.append(str(toplevel_path))
    return db_paths, f"{sources_dir}/*/AddressBook-v22.abcddb"


def query_addressbook_db(query: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Query the AddressBook database and return results as a list of dictionaries."""
    # Find the AddressBook database paths.
    db_paths, sources_pattern = _addressbook_db_paths()
    if not db_paths:
        return [
            {
                "error": (
                    "AddressBook database not found at "
                    f"{sources_pattern} {_FULL_DISK_ACCESS_HINT}"
                ),
            },
        ]

    # Try each database path until one works
    all_results: list[dict[str, Any]] = []
    for db_path in db_paths:
        results = _query_one_addressbook_db(db_path, query, params)
        if results is not None:
            all_results.extend(results)

    if not all_results:
        return [
            {
                "error": (
                    "Could not access any AddressBook databases. Please grant "
                    f"Full Disk Access permission. {_FULL_DISK_ACCESS_HINT}"
                ),
            },
        ]

    return all_results


def get_addressbook_contacts() -> dict[str, str]:
    """Query the macOS AddressBook for contacts and their phone numbers.

    Returns a dictionary mapping normalized phone numbers to contact names.
    """
    try:
        return _load_addressbook_contacts()
    except (OSError, sqlite3.Error, ValueError):
        logger.exception("Error getting AddressBook contacts")
        return {}


def _load_addressbook_contacts() -> dict[str, str]:
    """Read phone- and email-based contacts out of the AddressBook.

    With USE_TEST_DATA=true a single fixture contact is returned instead of
    reading the database, for hosts without Full Disk Access.
    """
    # Contact names, nicknames, and phone numbers
    phone_query = """
    SELECT
        ZABCDRECORD.ZFIRSTNAME as first_name,
        ZABCDRECORD.ZLASTNAME as last_name,
        ZABCDRECORD.ZNICKNAME as nickname,
        ZABCDPHONENUMBER.ZFULLNUMBER as phone
    FROM
        ZABCDRECORD
        LEFT JOIN ZABCDPHONENUMBER ON ZABCDRECORD.Z_PK = ZABCDPHONENUMBER.ZOWNER
    WHERE
        ZABCDPHONENUMBER.ZFULLNUMBER IS NOT NULL
    ORDER BY
        ZABCDRECORD.ZLASTNAME,
        ZABCDRECORD.ZFIRSTNAME,
        ZABCDPHONENUMBER.ZORDERINGINDEX ASC
    """

    # Query for email-based contacts (used by iMessage when handle is an email)
    email_query = """
    SELECT
        ZABCDRECORD.ZFIRSTNAME as first_name,
        ZABCDRECORD.ZLASTNAME as last_name,
        ZABCDRECORD.ZNICKNAME as nickname,
        ZABCDEMAILADDRESS.ZADDRESS as email
    FROM
        ZABCDRECORD
        LEFT JOIN ZABCDEMAILADDRESS ON ZABCDRECORD.Z_PK = ZABCDEMAILADDRESS.ZOWNER
    WHERE
        ZABCDEMAILADDRESS.ZADDRESS IS NOT NULL
    ORDER BY
        ZABCDRECORD.ZLASTNAME,
        ZABCDRECORD.ZFIRSTNAME
    """

    # For testing/fallback, parse the user-provided examples in cases where
    # direct DB access fails. This is a temporary workaround until full disk
    # access is granted.
    if os.environ.get("USE_TEST_DATA", "").lower() == "true":
        contacts = [
            {"first_name": "TEST", "last_name": "TEST", "phone": "+11111111111"},
        ]
        return process_contacts(contacts)

    # Try to query database directly
    results = query_addressbook_db(phone_query)

    if results and "error" in results[0]:
        logger.error("Error getting AddressBook contacts: %s", results[0]["error"])
        return {}

    # Also query email addresses for email-based iMessage handles
    email_results = query_addressbook_db(email_query)
    if email_results and "error" not in email_results[0]:
        results.extend(email_results)

    return process_contacts(results)


def process_contacts(contacts: list[dict[str, Any]]) -> dict[str, str]:
    """Process contact records into a normalized phone -> name map."""
    contacts_map: dict[str, str] = {}
    # Stores first_name, last_name, nickname for fuzzy matching
    phone_to_details: dict[str, dict[str, str]] = {}

    for contact in contacts:
        try:
            record = _parse_contact_row(contact)
        except (AttributeError, KeyError, TypeError, ValueError) as e:
            # Skip individual entries that fail to process
            logger.warning("Error processing contact: %s", e)
            continue
        if record is None:
            continue
        key, full_name, details = record
        contacts_map[key] = full_name
        phone_to_details[key] = details

    # Publish the detailed phone map for fuzzy matching
    global _PHONE_TO_DETAILS_MAP
    _PHONE_TO_DETAILS_MAP = phone_to_details

    return contacts_map


def _parse_contact_row(
    contact: dict[str, Any],
) -> tuple[str, str, dict[str, str]] | None:
    """Reduce one AddressBook row to (lookup key, full name, details).

    Returns None for rows without a usable name and for phone entries the
    parser cannot read. Email-only rows key on the lowercased address.
    """
    first_name = contact.get("first_name", "") or ""
    last_name = contact.get("last_name", "") or ""
    nickname = contact.get("nickname", "") or ""
    phone = contact.get("phone", "")
    email = contact.get("email", "")

    # Create full name
    full_name = " ".join(filter(None, [first_name, last_name]))
    if not full_name.strip():
        return None

    details = {
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "nickname": nickname.strip(),
        "full_name": full_name,
    }

    # Handle email-based contacts (iMessage handles can be email addresses)
    if email and not phone:
        return email.strip().lower(), full_name, details

    # Skip entries without phone numbers
    if not phone:
        return None

    # Clean up phone number and remove any image metadata
    if "X-IMAGETYPE" in phone:
        phone = phone.split("X-IMAGETYPE")[0]

    # Key the map on the canonical (E.164) form so a number stored as
    # "06 39 98 00 01" in the address book matches the "+33639980001"
    # handle the Messages database recorded for the same person.
    # Short codes and entries the parser cannot read keep a digits-only
    # key rather than being dropped from the map entirely.
    normalized_phone = contact_key(phone)
    if not normalized_phone:
        return None
    return normalized_phone, full_name, details


# phone -> {first_name, last_name, nickname, full_name}
_PHONE_TO_DETAILS_MAP: dict[str, dict[str, str]] = {}


def get_cached_contacts() -> dict[str, str]:
    """Get cached contacts map or refresh if needed."""
    global _CONTACTS_CACHE, _LAST_CACHE_UPDATE

    current_time = time.time()
    if _CONTACTS_CACHE is None or (current_time - _LAST_CACHE_UPDATE) > _CACHE_TTL_S:
        _CONTACTS_CACHE = get_addressbook_contacts()
        _LAST_CACHE_UPDATE = current_time

    return _CONTACTS_CACHE


def find_contact_by_name(name: str) -> list[dict[str, Any]]:
    """Find contacts by name or nickname using fuzzy matching.

    Searches against:
    - Full name (first + last)
    - Nickname

    Args:
        name: The name or nickname to search for

    Returns:
        List of matching contacts (may be multiple if ambiguous)

    """
    contacts = get_cached_contacts()
    global _PHONE_TO_DETAILS_MAP

    # Build candidates: search both full name and nickname
    candidates: list[tuple[str, str]] = []
    for phone, contact_name in contacts.items():
        # Add full name as searchable
        candidates.append((contact_name, phone))

        # Add nickname as searchable (if exists)
        details = _PHONE_TO_DETAILS_MAP.get(phone, {})
        nickname = details.get("nickname", "")
        if nickname:
            candidates.append((nickname, phone))

    # Perform fuzzy matching
    matches = fuzzy_match(name, candidates)

    # Deduplicate by phone number, keeping highest score for each
    seen_phones: dict[str, dict[str, Any]] = {}
    for matched_name, phone, score in matches:
        if phone not in seen_phones or score > seen_phones[phone]["score"]:
            # Get the display name (full name, not nickname)
            display_name = contacts.get(phone, matched_name)
            display_phone = (
                phone if "@" in phone else (_format_phone_for_messages(phone) or phone)
            )
            seen_phones[phone] = {
                "name": display_name,
                "phone": display_phone,
                "score": score,
                "matched_on": matched_name,  # What actually matched (name or nickname)
            }

    # Convert to sorted list
    ranked = sorted(
        seen_phones.values(), key=operator.itemgetter("score"), reverse=True
    )

    # An exact name or nickname hit means the caller named one person. Keep
    # only those entries so "Emma White" is not reported as ambiguous against
    # "Jeremy White" and "Alex Whitaker", which merely resemble it. Partial
    # queries ("Andrew") have no exact hit and keep the full fuzzy list.
    exact = [m for m in ranked if _same_name(m["matched_on"], name)]
    return exact or ranked


def _same_name(a: str, b: str) -> bool:
    """Case- and whitespace-insensitive name equality."""
    return " ".join(str(a).split()).casefold() == " ".join(str(b).split()).casefold()


def _single_person(matches: list[dict[str, Any]]) -> bool:
    """True when every match is a different handle of the same contact.

    One person usually has several handles (a phone number plus one or more
    iMessage email addresses); those are not a disambiguation question.
    """
    return len(matches) > 1 and all(
        _same_name(m["name"], matches[0]["name"]) for m in matches
    )


def _preferred_handle(matches: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the handle to send to: the phone number when there is one."""
    for m in matches:
        if "@" not in str(m["phone"]):
            return m
    return matches[0]


# Shared disambiguation store for `contact:N` selectors.
# Previously each consumer kept its own function-attribute cache
# (`send_message.recent_matches`, `get_recent_messages.recent_matches`) and
# `tool_find_contact` wrote to neither, so a selector printed by one tool
# could never resolve in another. One module-level list, capped to the
# entries actually shown to the user (<=10).
_recent_contact_matches: list[dict[str, Any]] = []


def set_recent_contact_matches(matches: list[dict[str, Any]]) -> None:
    """Record the disambiguation list shown to the user."""
    global _recent_contact_matches
    _recent_contact_matches = matches[:10].copy()


def get_recent_contact_matches() -> list[dict[str, Any]]:
    """Return the last disambiguation list shown to the user."""
    return _recent_contact_matches


def create_contact(name: str, phone: str, *, label: str = "mobile") -> str:
    """Create one Contacts.app entry holding a single phone number.

    This writes through Contacts.app automation, not the read-only AddressBook
    SQLite path, so it needs Automation permission for Contacts and is a
    privileged side effect the MCP client must gate like a send. It never merges
    with an existing card: calling it twice for the same person creates two
    entries.
    """
    if _policy.read_only():
        return (
            f"Error: this server is read-only ({_policy.READ_ONLY_VAR}); "
            "no contact was created."
        )
    name = str(name).strip()
    if not name:
        return "Error: name cannot be empty."
    phone = str(phone).strip()
    if not phone:
        return "Error: phone cannot be empty."

    dialable = to_dialable_e164(phone)
    stored_phone = dialable or phone
    parts = name.split()
    safe_first = _applescript.escape_applescript(parts[0])
    safe_last = _applescript.escape_applescript(" ".join(parts[1:]))
    safe_label = _applescript.escape_applescript(str(label or "mobile"))
    safe_phone = _applescript.escape_applescript(stored_phone)

    # The Contacts record literal needs doubled braces inside the f-string.
    script = f"""
    tell application "Contacts"
        set newPerson to make new person with properties {{first name:"{safe_first}", last name:"{safe_last}"}}
        make new phone at end of phones of newPerson with properties {{label:"{safe_label}", value:"{safe_phone}"}}
        save
        return "success"
    end tell
    """

    try:
        result = _applescript.run_applescript(script)
    except (OSError, subprocess.SubprocessError) as e:
        return f"Error creating contact: {e!s}"

    if result.startswith("Error:"):
        return f"Error creating contact: {result[6:].strip()}"
    if result.strip() != "success":
        return f"Unknown Contacts result: {result}"

    return (
        f"Created contact {neutralize_untrusted_text(name)} with "
        f"{neutralize_untrusted_text(str(label or 'mobile'))} number "
        f"{neutralize_untrusted_text(stored_phone)}"
    )


def _candidate_handles(recipient: str) -> list[str]:
    """Build the handle ids the Messages database might have recorded.

    Email handles are used as-is; phone numbers get every format the
    Messages database could have stored them under.
    """
    return handle_variants(recipient)


# handle ROWID -> display name, per database file. Filled in batches by
# prime_contact_names so a page of messages costs one handle query instead of
# one or two per rendered line. Dropped whenever the AddressBook cache
# refreshes, so a renamed contact shows up within _CACHE_TTL_S like before.
_HANDLE_NAME_CACHE: dict[tuple[str, int], str] = {}
_HANDLE_ADDRESS_CACHE: dict[tuple[str, int], str] = {}
_HANDLE_NAME_CACHE_STAMP: float = -1.0

# Fallback for handles the AddressBook does not know: the display name of a
# one-to-one chat they belong to (Messages for Business sets these). Group
# chats are excluded so an unknown sender is not labelled with the group's
# name.
_CHAT_NAME_FALLBACK_QUERY = """
    SELECT h.id AS address, c.display_name AS display_name
    FROM handle h
    JOIN chat_handle_join chj ON h.ROWID = chj.handle_id
    JOIN chat c ON chj.chat_id = c.ROWID
    WHERE h.id IN ({placeholders})
      AND c.display_name IS NOT NULL AND c.display_name != ''
      AND (c.style IS NULL OR c.style != 43)
"""


def _sync_handle_name_cache(contacts_map_stamp: float) -> None:
    global _HANDLE_NAME_CACHE_STAMP
    if contacts_map_stamp != _HANDLE_NAME_CACHE_STAMP:
        _HANDLE_NAME_CACHE.clear()
        _HANDLE_ADDRESS_CACHE.clear()
        _HANDLE_NAME_CACHE_STAMP = contacts_map_stamp


def clear_handle_name_cache() -> None:
    """Forget resolved handle names (tests, or after contacts change)."""
    global _HANDLE_NAME_CACHE_STAMP
    _HANDLE_NAME_CACHE.clear()
    _HANDLE_ADDRESS_CACHE.clear()
    _HANDLE_NAME_CACHE_STAMP = -1.0
    _CANONICAL_HANDLE_CACHE.clear()


def _name_from_addressbook(address: str, contacts_map: dict[str, str]) -> str | None:
    # Both sides of this comparison are canonical, so a handle recorded as
    # "+33639980001" matches an address book entry written "06 39 98 00 01"
    # without having to enumerate country-code variations by hand. The extra
    # keys cover the address book entries that could not be canonicalized and
    # so are stored under their digits.
    for key in lookup_keys(address):
        if key in contacts_map:
            return contacts_map[key]
    return None


def prime_contact_names(handle_ids: Any) -> None:
    """Resolve display names for many handle ROWIDs with two queries at most.

    After this, ``get_contact_name`` answers from the cache for each of them.
    Rows the database cannot serve are left unresolved (not cached as
    "Unknown"), so a transient access error does not stick for five minutes.
    """
    wanted = sorted({int(h) for h in handle_ids if h is not None})
    if not wanted:
        return
    db_path = _db.get_messages_db_path()
    contacts_map = get_cached_contacts()
    _sync_handle_name_cache(_LAST_CACHE_UPDATE)
    missing = [h for h in wanted if (db_path, h) not in _HANDLE_NAME_CACHE]
    if not missing:
        return

    placeholders = ", ".join("?" for _ in missing)
    rows = _db.query_messages_db(
        f"SELECT ROWID, id FROM handle WHERE ROWID IN ({placeholders})",
        tuple(missing),
    )
    if not rows or "error" in rows[0]:
        if rows and "error" in rows[0]:
            return
        rows = []
    addresses: dict[int, str] = {}
    for row in rows:
        if "id" not in row:
            continue
        rowid = row.get("ROWID")
        if rowid is None and len(missing) == 1:
            rowid = missing[0]
        if rowid is not None:
            addresses[int(rowid)] = str(row["id"])
            _HANDLE_ADDRESS_CACHE[(db_path, int(rowid))] = str(row["id"])

    unresolved: dict[int, str] = {}
    for handle_id in missing:
        address = addresses.get(handle_id)
        if address is None:
            _HANDLE_NAME_CACHE[(db_path, handle_id)] = "Unknown"
            continue
        name = _name_from_addressbook(address, contacts_map)
        if name is not None:
            _HANDLE_NAME_CACHE[(db_path, handle_id)] = name
        else:
            unresolved[handle_id] = address

    if not unresolved:
        return
    placeholders = ", ".join("?" for _ in unresolved)
    rows = _db.query_messages_db(
        _CHAT_NAME_FALLBACK_QUERY.format(placeholders=placeholders),
        tuple(unresolved.values()),
    )
    chat_names: dict[str, str] = {}
    if rows and "error" not in rows[0]:
        for row in rows:
            address = row.get("address")
            if (
                address is not None
                and address not in chat_names
                and row.get("display_name")
            ):
                chat_names[str(address)] = str(row["display_name"])
    for handle_id, address in unresolved.items():
        # No contact name found: show the phone number or email itself.
        _HANDLE_NAME_CACHE[(db_path, handle_id)] = chat_names.get(address, address)


def get_handle_address(handle_id: int | None) -> str | None:
    """The phone number or email behind a handle ROWID, or None if unknown."""
    if handle_id is None:
        return None
    db_path = _db.get_messages_db_path()
    _sync_handle_name_cache(_LAST_CACHE_UPDATE)
    key = (db_path, int(handle_id))
    if key not in _HANDLE_ADDRESS_CACHE and key not in _HANDLE_NAME_CACHE:
        prime_contact_names([handle_id])
    return _HANDLE_ADDRESS_CACHE.get(key)


def get_contact_name(handle_id: int | None) -> str:
    """Get contact name from handle_id with improved contact lookup.

    Answers from the cache ``prime_contact_names`` fills; a cold single lookup
    primes just that one handle.
    """
    if handle_id is None:
        return "Unknown"
    db_path = _db.get_messages_db_path()
    _sync_handle_name_cache(_LAST_CACHE_UPDATE)
    cached = _HANDLE_NAME_CACHE.get((db_path, int(handle_id)))
    if cached is None:
        prime_contact_names([handle_id])
        cached = _HANDLE_NAME_CACHE.get((db_path, int(handle_id)))
    return cached if cached is not None else "Unknown"


# AddressBook matches below this score are fuzzy-floor noise, outranked by an
# exact chat display-name match. Genuine hits score 0.80+ (exact token 0.95,
# prefix 0.80-0.85); see fuzzy_match.
_WEAK_MATCH_CEILING = 0.70


def _all_weak_matches(matches: list[dict[str, Any]]) -> bool:
    """Return True when every AddressBook match scores at or below the noise ceiling."""
    return bool(matches) and all(
        m.get("score", 0) <= _WEAK_MATCH_CEILING for m in matches
    )


def _handles_for_address(address: str) -> list[int]:
    """Return every handle ROWID for one phone number or email address.

    An email is matched case-insensitively (handle.id compares case-sensitively
    and the canonical form is lowercased). A phone number is tried in its
    various stored formats and returns all handles across protocols.
    """
    if "@" in address:
        results = _db.query_messages_db(
            "SELECT ROWID FROM handle WHERE id = ? COLLATE NOCASE",
            (canonical_handle(address) or address.strip(),),
        )
        if results and "error" not in results[0]:
            return [row["ROWID"] for row in results]
        return []
    return find_handles_by_phone(address) or []


def _get_phone_formats(recipient: str) -> list[str]:
    """Get the handle id formats a phone recipient may be stored under.

    Args:
        recipient: Phone recipient, in any format

    Returns:
        List of phone recipients in various formats, most canonical first.
        National-format numbers are expanded against the region this Mac is
        configured for rather than assumed to be North American.

    """
    return handle_variants(recipient)


def find_handle_by_phone(phone: str) -> int | None:
    """Find a handle ID by phone number, trying various formats.

    Prioritizes direct message handles over group chat handles.

    Args:
        phone: Phone number in any format

    Returns:
        handle_id if found, None otherwise

    """
    handles = find_handles_by_phone(phone)
    if handles:
        return handles[0]
    return None


def find_handles_by_phone(phone: str) -> list[int] | None:
    """Find all handle IDs by phone number, trying various formats.

    Returns all handles for multi-protocol support (iMessage, SMS, RCS).

    Args:
        phone: Phone number in any format

    Returns:
        List of handle_id's if found, None otherwise

    """
    formats_to_try = _get_phone_formats(phone)
    if not formats_to_try:
        return None

    placeholders = ", ".join(["?" for _ in formats_to_try])

    # Finds all handle_id's associated with the number
    query = f"""
    SELECT
    ROWID
    FROM handle
    WHERE id IN ({placeholders})
    """

    results = _db.query_messages_db(query, tuple(formats_to_try))

    if results and "error" not in results[0]:
        rowids = [row["ROWID"] for row in results]
        if rowids:
            return rowids

    # Nothing matched one of the shapes we can predict, so fall back to
    # comparing every handle on its canonical form. This catches numbers
    # Messages stored in a spelling the variant list does not anticipate.
    return _find_handles_by_canonical_form(phone)


def _find_handles_by_canonical_form(phone: str) -> list[int] | None:
    """Find handles holding the same number as `phone` in another format.

    Args:
        phone: Phone number in any format

    Returns:
        List of handle_id's if any handle reduces to the same E.164 number,
        None otherwise.

    Notes:
        Scans the whole handle table, which holds a few thousand rows at most,
        so this stays well under a millisecond per call once the canonical
        forms are cached. Only reached when the indexed lookup found nothing.

    """
    target = canonical_handle(phone)
    if not target:
        return None

    canonical_map = _canonical_handle_map()
    if canonical_map is None:
        return None
    return canonical_map.get(target) or None


# Per database file: (handle-table fingerprint, {canonical form: [ROWIDs]}).
# Rebuilt only when the handle table changes, so repeated misses do not each
# re-read and re-canonicalise every handle.
_CANONICAL_HANDLE_CACHE: dict[str, tuple[tuple[Any, Any], dict[str, list[int]]]] = {}


def _canonical_handle_map() -> dict[str, list[int]] | None:
    db_path = _db.get_messages_db_path()
    fingerprint_rows = _db.query_messages_db(
        "SELECT COUNT(*) AS n, COALESCE(MAX(ROWID), 0) AS max_rowid FROM handle"
    )
    if not fingerprint_rows or "error" in fingerprint_rows[0]:
        return None
    fingerprint = (
        fingerprint_rows[0].get("n"),
        fingerprint_rows[0].get("max_rowid"),
    )
    cached = _CANONICAL_HANDLE_CACHE.get(db_path)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]

    rows = _db.query_messages_db("SELECT ROWID, id FROM handle")
    if not rows or "error" in rows[0]:
        return None
    canonical_map: dict[str, list[int]] = {}
    for row in rows:
        canonical = canonical_handle(row.get("id", ""))
        if canonical:
            canonical_map.setdefault(canonical, []).append(row["ROWID"])
    _CANONICAL_HANDLE_CACHE[db_path] = (fingerprint, canonical_map)
    return canonical_map


def _describe_addressbook_schema(db_path: str) -> list[str]:
    """List one AddressBook copy's table and contact counts."""
    conn = _db._connect_sqlite_readonly(db_path)
    lines = [f"Successfully connected to database: {db_path}"]
    cursor = conn.cursor()
    cursor.execute("SELECT count(*) FROM sqlite_master")
    lines.append(f"Database contains {cursor.fetchone()[0]} tables")
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('ZABCDRECORD', 'ZABCDPHONENUMBER')",
    )
    tables = [row[0] for row in cursor.fetchall()]
    if "ZABCDRECORD" in tables and "ZABCDPHONENUMBER" in tables:
        lines.append("Required tables (ZABCDRECORD, ZABCDPHONENUMBER) are present")
    else:
        lines.append(
            f"WARNING: Some required tables are missing. Found: {', '.join(tables)}",
        )
    try:
        cursor.execute("SELECT COUNT(*) FROM ZABCDRECORD")
    except sqlite3.OperationalError:
        lines.append(f"Could not query contact count {_FULL_DISK_ACCESS_HINT}")
    else:
        lines.append(f"Database contains {cursor.fetchone()[0]} contacts")
    conn.close()
    return lines


def _describe_addressbook_db(db_path: str) -> list[str]:
    """Check one AddressBook copy's readability and schema for the report."""
    read_error = _db._unreadable_file_error(db_path)
    if read_error is not None:
        message = read_error.replace(
            "when trying to read ",
            f"when trying to read {db_path}: ",
        )
        return [message]
    lines = [f"File is readable: {db_path}"]

    # Try to connect to the database
    try:
        lines.extend(_describe_addressbook_schema(db_path))
    except sqlite3.Error as e:
        lines.append(
            f"ERROR: Database connection error for {db_path}: {e!s} "
            f"{_FULL_DISK_ACCESS_HINT}",
        )
    return lines


def check_addressbook_access() -> str:
    """Check whether the AddressBook database is accessible, with details."""
    home_dir = Path("~").expanduser()
    sources_dir = home_dir / "Library/Application Support/AddressBook/Sources"

    # Check if the directory exists
    if not sources_dir.exists():
        return (
            f"ERROR: AddressBook Sources directory not found at {sources_dir} "
            f"{_FULL_DISK_ACCESS_HINT}"
        )

    status = [f"AddressBook Sources directory exists at: {sources_dir}"]

    # Find database files
    db_paths = [str(path) for path in sources_dir.glob("*/AddressBook-v22.abcddb")]

    if not db_paths:
        return (
            f"ERROR: No AddressBook database files found in {sources_dir} "
            f"{_FULL_DISK_ACCESS_HINT}"
        )

    status.append(f"Found {len(db_paths)} AddressBook database files:")
    status.extend(f" - {path}" for path in db_paths)

    # Check file permissions and schemas for each database
    for db_path in db_paths:
        status.extend(_describe_addressbook_db(db_path))

    # Try to get actual contacts
    contacts = get_addressbook_contacts()
    if contacts:
        status.append(
            f"Successfully retrieved {len(contacts)} contacts with phone numbers",
        )
    else:
        status.append(
            f"WARNING: No contacts with phone numbers found. "
            f"{_FULL_DISK_ACCESS_HINT}",
        )

    return "\n".join(status)
