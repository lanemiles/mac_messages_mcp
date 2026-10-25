#!/usr/bin/env python3
# Copyright (c) 2023 Carter Lasalle
"""Regenerate ``mac_messages_mcp/messages.py``, the package's public facade.

The implementation lives in the core modules listed in ``MODULES``; the facade
re-exports every *public* name they define so
``from mac_messages_mcp.messages import ...`` stays the one import surface for
the server, the CLI, and downstream users. Private (``_``-prefixed) names are
not re-exported: reach those from the module that owns them.

Usage::

    python scripts/gen_facade.py          # rewrite messages.py
    python scripts/gen_facade.py --check  # exit 1 if messages.py is stale

``tests/test_structure.py`` runs the ``--check`` mode, so adding a public name
to a core module without regenerating the facade fails the suite.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "mac_messages_mcp"

# Order matters only for readability of the generated file.
MODULES = [
    "applescript",
    "db",
    "typedstream",
    "output",
    "payloads",
    "contacts",
    "chats",
    "semantics",
    "scope",
    "read",
    "threads",
    "send",
    "attachments",
]

# Public helpers that live outside the core modules but belong on the facade.
PHONE = [
    "canonical_handle",
    "contact_key",
    "digits_only",
    "get_default_region",
    "handle_variants",
    "is_email_handle",
    "lookup_keys",
    "to_dialable_e164",
]
UNTRUSTED = ["bound_untrusted_output", "neutralize_untrusted_text"]

# Module-level names that are implementation details even though public.
SKIP = {"logger", "annotations"}


def public_names(module: str) -> list[str]:
    names: set[str] = set()
    tree = ast.parse((PKG / f"{module}.py").read_text())
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return sorted(n for n in names if not n.startswith("_") and n not in SKIP)


def render() -> str:
    out = [
        "# Copyright (c) 2023 Carter Lasalle\n",
        '"""Core functionality for interacting with macOS Messages app.\n\n',
        "This module is a facade: the implementation lives in the core modules\n",
        "(``db``, ``contacts``, ``chats``, ``read``, ``send``, ``attachments`` and\n",
        "friends). Every public name they define is re-exported here so\n",
        "``from mac_messages_mcp.messages import ...`` keeps working for the server,\n",
        "the CLI, and downstream users. Private helpers are not re-exported; import\n",
        "them from the module that owns them.\n\n",
        "GENERATED FILE. Regenerate with ``python scripts/gen_facade.py`` after\n",
        "adding a public name to a core module; ``--check`` verifies it is current.\n",
        '"""\n\n',
    ]
    # Alphabetical by module and name, exploded with trailing commas: the
    # output is already black- and isort-stable, so --check never fights them.
    exports: dict[str, list[str]] = {m: public_names(m) for m in MODULES}
    exports["phone"] = sorted(PHONE)
    exports["untrusted"] = sorted(UNTRUSTED)
    all_names: list[str] = []
    for module in sorted(exports):
        names = exports[module]
        if not names:
            continue
        all_names += names
        out.append(
            f"from .{module} import (\n" + "".join(f"    {n},\n" for n in names) + ")\n"
        )
    out.append(
        "\n__all__ = [\n"
        + "".join(f'    "{n}",\n' for n in sorted(set(all_names)))
        + "]\n"
    )
    return "".join(out)


def main(argv: list[str]) -> int:
    target = PKG / "messages.py"
    rendered = render()
    if "--check" in argv:
        current = target.read_text() if target.exists() else ""
        if current != rendered:
            print(
                "mac_messages_mcp/messages.py is stale; "
                "run: python scripts/gen_facade.py",
                file=sys.stderr,
            )
            return 1
        print("messages.py is current")
        return 0
    target.write_text(rendered)
    print(f"wrote {target.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
