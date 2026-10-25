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
        "to_apple_ns",
        "from_apple_ns",
        "resolve_scope",
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


# --- Round 2 #11: public facade, committed generator, whole-package types ---


def _facade_names() -> set[str]:
    names: set[str] = set()
    for node in ast.parse(_source("messages")).body:
        if isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def test_facade_exports_only_the_public_api():
    """``messages`` is the import surface for the server, CLI, and downstream
    users. Private helpers are reached from the module that owns them, so the
    facade does not freeze every ``_internal`` as public API by accident."""
    private = sorted(n for n in _facade_names() if n.startswith("_"))
    assert private == [], f"facade re-exports private names: {private}"


def test_nothing_imports_private_names_through_the_facade():
    offenders = []
    for path in [*Path(__file__).parent.glob("*.py"), *PACKAGE.glob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module in (
                "mac_messages_mcp.messages",
                ".messages",
                "messages",
            ):
                offenders += [
                    f"{path.name}: {a.name}"
                    for a in node.names
                    if a.name.startswith("_")
                ]
    assert offenders == [], offenders


def test_facade_generator_is_committed_and_current():
    """The facade is generated. The generator lives in the repo and its
    ``--check`` mode confirms ``messages.py`` matches what it would emit, so a
    new public name cannot be added to a core module and forgotten here."""
    import subprocess
    import sys

    script = PACKAGE.parent / "scripts" / "gen_facade.py"
    assert script.exists(), "scripts/gen_facade.py is missing"
    result = subprocess.run(
        [sys.executable, str(script), "--check"],
        cwd=PACKAGE.parent,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("workflow", ["ci.yml", "publish.yml"])
def test_ci_type_checks_the_whole_package(workflow):
    """mypy on ``server.py`` alone with ``--follow-imports=skip`` checks one
    file of twenty-six; a use-before-definition in ``read.py`` shipped past it."""
    text = (PACKAGE.parent / ".github" / "workflows" / workflow).read_text()
    mypy_lines = [line for line in text.splitlines() if "mypy" in line]
    assert mypy_lines, f"{workflow} does not run mypy"
    for line in mypy_lines:
        assert "mac_messages_mcp/server.py" not in line, line
        assert re.search(r"\bmac_messages_mcp/?(\s|$)", line), line
