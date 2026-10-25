# Copyright (c) 2023 Carter Lasalle
"""Server-side limits on the privileged write path.

``tool_send_message`` is reachable by any agent connected to the server, and a
prompt-injected message can steer that agent. These tests pin the controls
that bound the damage: which files may be attached, a read-only mode, a
mandatory human confirmation, an hourly send cap, and a recipient allowlist.
"""

from __future__ import annotations

import importlib
import os
from unittest.mock import patch

import pytest

from mac_messages_mcp import contacts, policy, send
from mac_messages_mcp.messages import create_contact, send_message
from mac_messages_mcp.untrusted import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

ALICE = "+15555550100"


def _inner(fenced: str) -> str:
    assert fenced.startswith(UNTRUSTED_OPEN) and fenced.endswith(UNTRUSTED_CLOSE)
    return (
        fenced[len(UNTRUSTED_OPEN) : -len(UNTRUSTED_CLOSE)].strip().replace("\\n", "\n")
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake home directory with the default attachment roots and some secrets."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MAC_MESSAGES_ATTACHMENT_ROOTS", raising=False)
    for sub in (
        "Desktop",
        "Downloads",
        "Documents",
        "Pictures",
        ".ssh",
        "Library/Messages",
    ):
        (tmp_path / sub).mkdir(parents=True)
    (tmp_path / "Desktop" / "photo.jpg").write_bytes(b"jpg")
    (tmp_path / "Desktop" / ".env").write_text("SECRET=1")
    (tmp_path / ".ssh" / "id_ed25519").write_text("PRIVATE KEY")
    (tmp_path / "Library" / "Messages" / "chat.db").write_bytes(b"sqlite")
    (tmp_path / "elsewhere.txt").write_text("x")
    return tmp_path


@pytest.fixture
def applescript_ok():
    with patch(
        "mac_messages_mcp.applescript.run_applescript", return_value="success"
    ) as run:
        with patch(
            "mac_messages_mcp.send._report_send_outcome",
            return_value="Message sent successfully via iMessage to Alice",
        ):
            yield run


# ---------------------------------------------------------------------------
# Attachments: only from allowed folders, never dotfiles or ~/Library
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [".ssh/id_ed25519", "Library/Messages/chat.db", "elsewhere.txt", "Desktop/.env"],
)
def test_attachments_outside_the_allowed_roots_are_refused(
    home, applescript_ok, secret
):
    result = send_message(ALICE, "here you go", attachment_paths=[str(home / secret)])
    assert result.startswith("Error"), result
    assert "MAC_MESSAGES_ATTACHMENT_ROOTS" in result
    applescript_ok.assert_not_called()


def test_attachment_under_an_allowed_root_is_sent(home, applescript_ok):
    result = send_message(
        ALICE, "here you go", attachment_paths=[str(home / "Desktop" / "photo.jpg")]
    )
    assert "photo.jpg sent" in result
    assert applescript_ok.called


def test_symlink_into_a_forbidden_place_is_refused(home, applescript_ok):
    link = home / "Downloads" / "innocent.txt"
    link.symlink_to(home / ".ssh" / "id_ed25519")
    result = send_message(ALICE, "x", attachment_paths=[str(link)])
    assert result.startswith("Error") and "MAC_MESSAGES_ATTACHMENT_ROOTS" in result
    applescript_ok.assert_not_called()


def test_roots_can_be_configured_and_star_disables_the_check(
    home, applescript_ok, monkeypatch
):
    monkeypatch.setenv("MAC_MESSAGES_ATTACHMENT_ROOTS", str(home / "elsewhere_dir"))
    (home / "elsewhere_dir").mkdir()
    (home / "elsewhere_dir" / "f.txt").write_text("x")
    assert "f.txt sent" in send_message(
        ALICE, "x", attachment_paths=[str(home / "elsewhere_dir" / "f.txt")]
    )
    assert send_message(
        ALICE, "x", attachment_paths=[str(home / "Desktop" / "photo.jpg")]
    ).startswith("Error")

    monkeypatch.setenv("MAC_MESSAGES_ATTACHMENT_ROOTS", "*")
    assert "id_ed25519 sent" in send_message(
        ALICE, "x", attachment_paths=[str(home / ".ssh" / "id_ed25519")]
    )


# ---------------------------------------------------------------------------
# Read-only mode
# ---------------------------------------------------------------------------


def test_read_only_mode_refuses_every_write(monkeypatch, applescript_ok):
    monkeypatch.setenv("MAC_MESSAGES_READ_ONLY", "1")
    assert "read-only" in send_message(ALICE, "hi")
    assert "read-only" in create_contact("New Person", ALICE)
    applescript_ok.assert_not_called()


def test_read_only_mode_does_not_register_write_tools(monkeypatch):
    from mac_messages_mcp import server

    monkeypatch.setenv("MAC_MESSAGES_READ_ONLY", "1")
    try:
        reloaded = importlib.reload(server)
        names = {t.name for t in reloaded.mcp._tool_manager.list_tools()}
        assert "tool_get_recent_messages" in names
        assert not names & {
            "tool_send_message",
            "tool_create_contact",
            "tool_schedule_message",
        }, names
    finally:
        monkeypatch.delenv("MAC_MESSAGES_READ_ONLY")
        importlib.reload(server)


# ---------------------------------------------------------------------------
# Mandatory confirmation
# ---------------------------------------------------------------------------


def test_require_confirm_forces_elicitation_even_when_the_agent_says_no(monkeypatch):
    import asyncio

    from mac_messages_mcp import server

    monkeypatch.setenv("MAC_MESSAGES_REQUIRE_CONFIRM", "1")
    with patch("mac_messages_mcp.server.send_message") as dispatch:
        result = _inner(
            asyncio.run(server.tool_send_message(ALICE, "hi", confirm=False, ctx=None))
        )
    assert "Nothing was sent" in result or "nothing was sent" in result
    dispatch.assert_not_called()


def test_require_confirm_refuses_unattended_scheduling(monkeypatch):
    from mac_messages_mcp import server

    monkeypatch.setenv("MAC_MESSAGES_REQUIRE_CONFIRM", "1")
    result = _inner(server.tool_schedule_message(ALICE, "later", "2999-01-01T09:00:00"))
    assert result.startswith("Error") and "MAC_MESSAGES_REQUIRE_CONFIRM" in result
    assert server.scheduler.list() == []


# ---------------------------------------------------------------------------
# Hourly send cap and recipient allowlist
# ---------------------------------------------------------------------------


def test_hourly_send_cap(monkeypatch, applescript_ok):
    monkeypatch.setenv("MAC_MESSAGES_MAX_SENDS_PER_HOUR", "2")
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(policy, "_now", lambda: clock["now"])
    policy.reset_send_budget()

    assert "successfully" in send_message(ALICE, "one")
    assert "successfully" in send_message(ALICE, "two")
    third = send_message(ALICE, "three")
    assert third.startswith("Error") and "2 sends per hour" in third
    assert applescript_ok.call_count == 2

    clock["now"] += 3601
    assert "successfully" in send_message(ALICE, "four")


def test_recipient_allowlist(monkeypatch, applescript_ok):
    monkeypatch.setenv(
        "MAC_MESSAGES_ALLOWED_RECIPIENTS", "+15555550100, meg@example.com, chat999"
    )
    assert "successfully" in send_message("(555) 555-0100", "national spelling matches")
    assert "successfully" in send_message("Meg@Example.com", "case-insensitive email")
    blocked = send_message("+15555550199", "stranger")
    assert blocked.startswith("Error") and "allowlist" in blocked
    assert applescript_ok.call_count == 2

    with patch(
        "mac_messages_mcp.send._send_via_temp_file",
        return_value="Message sent successfully to chat999",
    ) as group:
        assert "successfully" in send_message("chat999", "group ok", group_chat=True)
        assert send_message("chat123", "other group", group_chat=True).startswith(
            "Error"
        )
        assert group.call_count == 1


def test_policy_summary_is_reported_by_the_access_check(monkeypatch, chat_db):
    from mac_messages_mcp.messages import check_messages_db_access

    monkeypatch.setenv("MAC_MESSAGES_READ_ONLY", "1")
    monkeypatch.setenv("MAC_MESSAGES_MAX_SENDS_PER_HOUR", "5")
    report = check_messages_db_access()
    assert "read-only" in report and "5" in report
