# Copyright (c) 2023 Carter Lasalle
"""Core functionality for interacting with macOS Messages app.

This module is a facade: the implementation lives in the core modules
(``db``, ``contacts``, ``chats``, ``read``, ``send``, ``attachments`` and
friends). Every public name they define is re-exported here so
``from mac_messages_mcp.messages import ...`` keeps working for the server,
the CLI, and downstream users. Private helpers are not re-exported; import
them from the module that owns them.

GENERATED FILE. Regenerate with ``python scripts/gen_facade.py`` after
adding a public name to a core module; ``--check`` verifies it is current.
"""

from .applescript import (
    escape_applescript,
    run_applescript,
)
from .attachments import (
    ATTACHMENT_SELECT_COLS,
    attachment_select_cols,
    filter_excluded_attachments,
    get_attachment,
    search_attachments,
    shape_attachment,
)
from .chats import (
    clear_chat_caches,
    format_chat_label,
    get_chat_mapping,
    get_chat_participants,
    list_conversations,
    list_named_chats,
)
from .contacts import (
    check_addressbook_access,
    clean_name,
    clear_handle_name_cache,
    create_contact,
    expand_handles_by_person,
    find_contact_by_name,
    find_handle_by_phone,
    find_handles_by_phone,
    fuzzy_match,
    get_addressbook_contacts,
    get_cached_contacts,
    get_contact_name,
    get_handle_address,
    get_handle_person_id,
    get_recent_contact_matches,
    normalize_phone_number,
    prime_contact_names,
    process_contacts,
    query_addressbook_db,
    set_recent_contact_matches,
)
from .db import (
    APPLE_EPOCH_OFFSET,
    check_messages_db_access,
    close_cached_connections,
    date_range_clauses,
    format_local,
    from_apple_ns,
    get_messages_db_path,
    isoformat_local,
    local_timezone,
    query_messages_db,
    to_apple_ns,
)
from .output import (
    OUTPUT_FORMATS,
)
from .payloads import (
    decode_balloon,
    decode_summary_info,
)
from .phone import (
    canonical_handle,
    contact_key,
    digits_only,
    get_default_region,
    handle_variants,
    is_email_handle,
    lookup_keys,
    to_dialable_e164,
)
from .read import (
    fuzzy_search_messages,
    get_recent_messages,
    wait_for_new_messages,
)
from .scope import (
    Scope,
    resolve_scope,
)
from .send import (
    apple_scheduled_messages,
    send_message,
)
from .threads import (
    get_conversation,
)
from .typedstream import (
    extract_body_from_attributed,
)
from .untrusted import (
    bound_untrusted_output,
    neutralize_untrusted_text,
)

__all__ = [
    "APPLE_EPOCH_OFFSET",
    "ATTACHMENT_SELECT_COLS",
    "OUTPUT_FORMATS",
    "Scope",
    "apple_scheduled_messages",
    "attachment_select_cols",
    "bound_untrusted_output",
    "canonical_handle",
    "check_addressbook_access",
    "check_messages_db_access",
    "clean_name",
    "clear_chat_caches",
    "clear_handle_name_cache",
    "close_cached_connections",
    "contact_key",
    "create_contact",
    "date_range_clauses",
    "decode_balloon",
    "decode_summary_info",
    "digits_only",
    "escape_applescript",
    "expand_handles_by_person",
    "extract_body_from_attributed",
    "filter_excluded_attachments",
    "find_contact_by_name",
    "find_handle_by_phone",
    "find_handles_by_phone",
    "format_chat_label",
    "format_local",
    "from_apple_ns",
    "fuzzy_match",
    "fuzzy_search_messages",
    "get_addressbook_contacts",
    "get_attachment",
    "get_cached_contacts",
    "get_chat_mapping",
    "get_chat_participants",
    "get_contact_name",
    "get_conversation",
    "get_default_region",
    "get_handle_address",
    "get_handle_person_id",
    "get_messages_db_path",
    "get_recent_contact_matches",
    "get_recent_messages",
    "handle_variants",
    "is_email_handle",
    "isoformat_local",
    "list_conversations",
    "list_named_chats",
    "local_timezone",
    "lookup_keys",
    "neutralize_untrusted_text",
    "normalize_phone_number",
    "prime_contact_names",
    "process_contacts",
    "query_addressbook_db",
    "query_messages_db",
    "resolve_scope",
    "run_applescript",
    "search_attachments",
    "send_message",
    "set_recent_contact_matches",
    "shape_attachment",
    "to_apple_ns",
    "to_dialable_e164",
    "wait_for_new_messages",
]
