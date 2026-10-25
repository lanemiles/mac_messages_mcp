# Copyright (c) 2023 Carter Lasalle
"""Server-side limits on the privileged write path, configured by environment.

Every control here is enforced inside the send/create functions themselves,
not only at the MCP tool layer, so a scheduled send or a direct Python caller
is bound by the same rules. Values are read at call time, so a test or a
supervisor can change them without restarting.

Variables:

``MAC_MESSAGES_ATTACHMENT_ROOTS``
    Colon-separated folders files may be attached from. Default:
    ``~/Desktop:~/Downloads:~/Documents:~/Pictures``. Paths are resolved
    through symlinks before the check, and any path with a dot-prefixed
    component (``.ssh``, ``.env``) or under ``~/Library`` is refused unless the
    value is ``*``, which disables the check entirely.
``MAC_MESSAGES_READ_ONLY``
    ``1`` to refuse every send and contact write and to leave the write tools
    unregistered.
``MAC_MESSAGES_REQUIRE_CONFIRM``
    ``1`` to make ``tool_send_message`` ask the human through MCP elicitation on
    every call regardless of its ``confirm`` argument, and to refuse scheduled
    sends (which no human confirms).
``MAC_MESSAGES_MAX_SENDS_PER_HOUR``
    Integer cap on sends attempted in any rolling hour; ``0`` (default) is
    unlimited.
``MAC_MESSAGES_ALLOWED_RECIPIENTS``
    Comma-separated phone numbers (any spelling), email addresses, or chat IDs.
    When set, a send to anything else is refused.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from pathlib import Path

from .phone import canonical_handle

ATTACHMENT_ROOTS_VAR = "MAC_MESSAGES_ATTACHMENT_ROOTS"
READ_ONLY_VAR = "MAC_MESSAGES_READ_ONLY"
REQUIRE_CONFIRM_VAR = "MAC_MESSAGES_REQUIRE_CONFIRM"
MAX_SENDS_VAR = "MAC_MESSAGES_MAX_SENDS_PER_HOUR"
ALLOWED_RECIPIENTS_VAR = "MAC_MESSAGES_ALLOWED_RECIPIENTS"

DEFAULT_ATTACHMENT_ROOTS = ("~/Desktop", "~/Downloads", "~/Documents", "~/Pictures")

_TRUE = {"1", "true", "yes", "on"}


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def read_only() -> bool:
    """Whether every write is refused."""
    return _flag(READ_ONLY_VAR)


def require_confirm() -> bool:
    """Whether every send must be confirmed by the human through elicitation."""
    return _flag(REQUIRE_CONFIRM_VAR)


def max_sends_per_hour() -> int:
    raw = os.environ.get(MAX_SENDS_VAR, "").strip()
    try:
        return max(0, int(raw)) if raw else 0
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Attachment roots
# ---------------------------------------------------------------------------


def attachment_roots() -> list[Path] | None:
    """Resolved allowed folders, or None when the check is disabled (``*``)."""
    raw = os.environ.get(ATTACHMENT_ROOTS_VAR)
    if raw is not None and raw.strip() == "*":
        return None
    entries = raw.split(":") if raw and raw.strip() else list(DEFAULT_ATTACHMENT_ROOTS)
    roots: list[Path] = []
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        try:
            roots.append(Path(entry).expanduser().resolve())
        except OSError:
            continue
    return roots


def attachment_path_error(resolved: Path) -> str | None:
    """Why ``resolved`` (already symlink-resolved) may not be attached, or None."""
    roots = attachment_roots()
    if roots is None:
        return None
    home = Path(os.path.expanduser("~")).resolve()
    hint = (
        f" Allowed folders come from {ATTACHMENT_ROOTS_VAR} "
        f"(default: {':'.join(DEFAULT_ATTACHMENT_ROOTS)}); set it to * to disable."
    )
    if any(part.startswith(".") for part in resolved.parts):
        return f"Error: attachment refused, hidden file or folder: {resolved}.{hint}"
    library = home / "Library"
    if library == resolved or library in resolved.parents:
        return f"Error: attachment refused, inside ~/Library: {resolved}.{hint}"
    for root in roots:
        if root == resolved or root in resolved.parents:
            return None
    return f"Error: attachment refused, outside the allowed folders: {resolved}.{hint}"


# ---------------------------------------------------------------------------
# Recipient allowlist
# ---------------------------------------------------------------------------


def _canonical_recipient(value: str) -> str:
    value = value.strip()
    return canonical_handle(value) or value.lower()


def allowed_recipients() -> set[str] | None:
    """Canonical allowlist entries, or None when no allowlist is configured."""
    raw = os.environ.get(ALLOWED_RECIPIENTS_VAR)
    if raw is None or not raw.strip():
        return None
    return {_canonical_recipient(v) for v in raw.split(",") if v.strip()}


def recipient_error(recipient: str) -> str | None:
    """Why ``recipient`` (a resolved address or chat id) may not be messaged."""
    allowed = allowed_recipients()
    if allowed is None:
        return None
    if _canonical_recipient(recipient) in allowed:
        return None
    return (
        f"Error: recipient '{recipient}' is not on the send allowlist "
        f"({ALLOWED_RECIPIENTS_VAR})."
    )


# ---------------------------------------------------------------------------
# Hourly send budget
# ---------------------------------------------------------------------------

_WINDOW_SECONDS = 3600.0
_budget_lock = threading.Lock()
_send_times: deque[float] = deque()


def _now() -> float:
    return time.time()


def reset_send_budget() -> None:
    with _budget_lock:
        _send_times.clear()


def send_budget_error() -> str | None:
    """Record one send attempt; return an error when the hourly cap is hit.

    Attempts count, not successes: a flood of failing sends is still a flood.
    """
    cap = max_sends_per_hour()
    if cap <= 0:
        return None
    now = _now()
    with _budget_lock:
        while _send_times and now - _send_times[0] >= _WINDOW_SECONDS:
            _send_times.popleft()
        if len(_send_times) >= cap:
            return (
                f"Error: send refused, the limit of {cap} sends per hour "
                f"({MAX_SENDS_VAR}) has been reached."
            )
        _send_times.append(now)
    return None


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def summary_lines() -> list[str]:
    """Human-readable policy state for the access diagnostic."""
    roots = attachment_roots()
    cap = max_sends_per_hour()
    allowed = allowed_recipients()
    return [
        "Write policy:",
        f" - mode: {'read-only' if read_only() else 'read-write'}",
        f" - human confirmation: {'required on every send' if require_confirm() else 'client-gated'}",
        f" - sends per hour: {cap if cap else 'unlimited'}",
        f" - recipient allowlist: {len(allowed) if allowed is not None else 'none'}",
        " - attachment folders: "
        + (
            "any (check disabled)"
            if roots is None
            else ", ".join(str(r) for r in roots)
        ),
    ]
