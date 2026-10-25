# Copyright (c) 2023 Carter Lasalle
"""Documentation stays in step with the code.

A tool the server registers but the README and skill never mention is one an
agent will not know to call; a stale design-notes folder shipped in the sdist
is dead weight that misleads contributors.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
README = (REPO / "README.md").read_text()
SKILL = (REPO / ".claude" / "skills" / "mac-messages" / "SKILL.md").read_text()


def _registered_tools() -> set[str]:
    server = (REPO / "mac_messages_mcp" / "server.py").read_text()
    return set(re.findall(r"^(?:async )?def (tool_\w+)\(", server, re.M))


def test_every_registered_tool_is_in_the_readme_table():
    table_rows = set(re.findall(r"^\| `(tool_\w+)`", README, re.M))
    missing = sorted(_registered_tools() - table_rows)
    assert missing == [], f"README tools table is missing: {missing}"


def test_every_registered_tool_is_in_the_skill():
    missing = sorted(t for t in _registered_tools() if t not in SKILL)
    assert missing == [], f"SKILL.md never mentions: {missing}"


def test_readme_explains_an_always_on_mac_setup():
    """The common deployment is a Mac mini left running for an agent. The README
    says how to lock that down: confirm-every-send or read-only, a send cap,
    and a recipient allow-list, with a config block to copy."""
    section = re.search(r"^## .*[Aa]lways-on.*$", README, re.M)
    assert section, "no '## ... always-on ...' section in README"
    body = README[section.end() :].split("\n## ", 1)[0]
    for needle in (
        "MAC_MESSAGES_REQUIRE_CONFIRM",
        "MAC_MESSAGES_READ_ONLY",
        "MAC_MESSAGES_MAX_SENDS_PER_HOUR",
        "MAC_MESSAGES_ALLOWED_RECIPIENTS",
        "Full Disk Access",
    ):
        assert needle in body, needle


def test_stale_design_notes_are_gone():
    assert not (REPO / "memory-bank").exists(), "memory-bank/ should be deleted"
    assert "memory-bank" not in (REPO / "MANIFEST.in").read_text()
