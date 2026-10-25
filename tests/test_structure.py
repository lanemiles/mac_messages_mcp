# Copyright (c) 2023 Carter Lasalle
"""Shape of the package: module boundaries the refactor established.

These pin structure rather than behaviour, so a future change that quietly
grows ``messages.py`` back into a monolith, reaches into another module's
private names, or re-duplicates a query fails here.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "mac_messages_mcp"

# Modules the core logic is split into, each owning one concern.
CORE_MODULES = (
    "db",
    "output",
    "contacts",
    "chats",
    "read",
    "send",
    "attachments",
    "applescript",
)

# Generous ceiling: large enough for a real module, far below the 3,500-line
# monolith this replaced.
MAX_MODULE_LINES = 1_600


def _source(name: str) -> str:
    return (PACKAGE / f"{name}.py").read_text()


def test_core_modules_exist_and_import():
    for name in CORE_MODULES:
        assert (PACKAGE / f"{name}.py").exists(), f"missing module {name}.py"
        importlib.import_module(f"mac_messages_mcp.{name}")


def test_messages_is_a_thin_facade():
    """``messages`` only re-exports; the logic lives in the core modules."""
    tree = ast.parse(_source("messages"))
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # module docstring
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            continue
        pytest.fail(
            f"messages.py defines logic at line {node.lineno}: {ast.dump(node)[:80]}"
        )


@pytest.mark.parametrize("name", [p.stem for p in sorted(PACKAGE.glob("*.py"))])
def test_no_module_is_a_monolith(name):
    lines = len(_source(name).splitlines())
    assert lines <= MAX_MODULE_LINES, f"{name}.py is {lines} lines"


def test_facade_keeps_the_public_api():
    """Everything the server, CLI, and tests import from ``messages`` still resolves."""
    from mac_messages_mcp import messages

    for name in (
        "query_messages_db",
        "get_recent_messages",
        "fuzzy_search_messages",
        "list_conversations",
        "wait_for_new_messages",
        "send_message",
        "create_contact",
        "find_contact_by_name",
        "get_cached_contacts",
        "get_contact_name",
        "search_attachments",
        "get_attachment",
        "check_messages_db_access",
        "check_addressbook_access",
        "set_recent_contact_matches",
        "extract_body_from_attributed",
        "run_applescript",
        "escape_applescript",
        "_check_imessage_availability",
        "_describe_attachment",
        "_format_phone_for_messages",
        "_to_apple_ns",
        "_from_apple_ns",
    ):
        assert hasattr(messages, name), name


def test_content_uses_owning_modules_not_private_names():
    src = _source("content")
    assert not re.search(r"\bmessages\._", src), "content.py reaches into messages._*"
    assert "from . import messages" not in src


def test_one_date_range_builder():
    """Every date filter string-binds Apple-ns timestamps the same way."""
    offenders = [
        p.name
        for p in PACKAGE.glob("*.py")
        if "CAST(m.date AS INTEGER)" in p.read_text()
    ]
    assert offenders == [], f"integer-cast date filters remain in {offenders}"
    assert _source("db").count("def date_range_clauses") == 1


def test_named_chats_query_is_shared_by_server_and_cli():
    literal = "SELECT chat_identifier, display_name FROM chat"
    assert literal not in _source("server")
    assert literal not in _source("cli")
    from mac_messages_mcp import chats

    assert callable(getattr(chats, "list_named_chats", None))
