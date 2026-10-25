# Copyright (c) 2023 Carter Lasalle
"""Process-level state in one place.

The server keeps a handful of caches as module globals (sender names, chat
lookups, column probes, the SQLite connections, the send budget, the search
index). ``Session`` is the single handle on all of them: ``reset()`` clears
everything and ``status()`` reports their sizes, so tests and diagnostics do
not have to know which module owns which cache.
"""

from __future__ import annotations

from typing import Any

from . import attachments, chats, contacts, db, policy, read, search_index


class Session:
    """The caches this process holds, with one reset and one status."""

    def reset(self) -> None:
        contacts.clear_handle_name_cache()
        chats.clear_chat_caches()
        read._MESSAGE_COLUMNS_CACHE.clear()
        attachments._ATTACHMENT_COLUMNS_CACHE.clear()
        db.close_cached_connections()
        policy.reset_send_budget()
        search_index.reset()

    def status(self) -> dict[str, Any]:
        return {
            "handle_names_cached": len(contacts._HANDLE_NAME_CACHE),
            "chat_lookups_cached": sum(
                len(entry[1]) for entry in chats._CHAT_LOOKUP_CACHE.values()
            ),
            "chat_mappings_cached": len(chats._CHAT_MAPPING_CACHE),
            "message_column_probes": len(read._MESSAGE_COLUMNS_CACHE),
            "attachment_column_probes": len(attachments._ATTACHMENT_COLUMNS_CACHE),
            "db_connection_open": getattr(db._LOCAL, "connection", None) is not None,
            "search_index_open": search_index._connection is not None,
        }


def reset_all_caches() -> None:
    """Convenience for callers that only want the reset."""
    Session().reset()
