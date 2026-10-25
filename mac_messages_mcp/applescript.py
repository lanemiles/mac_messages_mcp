# Copyright (c) 2023 Carter Lasalle
"""Run and escape AppleScript for Messages.app and Contacts.app automation."""

from __future__ import annotations

import subprocess

_APPLESCRIPT_TIMEOUT_SECONDS = 30


def run_applescript(script: str, timeout: float = _APPLESCRIPT_TIMEOUT_SECONDS) -> str:
    """Run an AppleScript and return the result."""
    proc = subprocess.Popen(
        ["osascript", "-e", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    with proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return f"Error: AppleScript timed out after {timeout:g} seconds"
    if proc.returncode != 0:
        return f"Error: {err.decode('utf-8')}"
    return out.decode("utf-8").strip()


def escape_applescript(value: str | None) -> str:
    """Escape a string for safe interpolation into an AppleScript double-quoted string.

    Escapes backslashes first (so subsequent escapes aren't double-escaped), then
    double quotes, then control characters that would otherwise terminate the
    AppleScript string literal or break the script (newlines, carriage returns,
    tabs). Also handles Unicode line/paragraph separators (U+2028 / U+2029),
    which AppleScript treats as line terminators.
    """
    if value is None:
        return ""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r\n", "\\n")
        .replace("\r", "\\n")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\u2028", "\\n")
        .replace("\u2029", "\\n")
    )
