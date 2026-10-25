# Copyright (c) 2023 Carter Lasalle
"""The server keeps answering while a slow tool runs.

FastMCP calls a synchronous tool inline on the event loop, so a tool that
sleeps or scans a large table stops every other request (pings, cancels,
parallel calls) until it returns. ``tool_wait_for_new_messages`` sleeps for
up to 300 seconds by design; the first search over a never-indexed database
can take tens of seconds. Both must run off the loop.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

from mac_messages_mcp import server

ALICE = "+15555550100"


def _tool_names() -> list[str]:
    return sorted(t.name for t in server.mcp._tool_manager.list_tools())


def test_every_registered_tool_runs_as_a_coroutine():
    """Registered tools are coroutine functions, so FastMCP awaits them and the
    blocking body is delegated to a worker thread instead of the loop."""
    blocking = sorted(
        t.name for t in server.mcp._tool_manager.list_tools() if not t.is_async
    )
    assert blocking == [], f"tools that would block the event loop: {blocking}"


def test_tool_functions_stay_callable_synchronously(chat_db):
    """The Python API and existing tests call ``server.tool_*`` directly."""
    out = server.tool_check_db_access()
    assert isinstance(out, str) and "untrusted-mcp-output" in out


def test_a_waiting_tool_does_not_stall_other_calls(chat_db):
    chat_db.add_handle(ALICE)

    async def scenario() -> float:
        waiting = asyncio.ensure_future(
            server.mcp.call_tool(
                "tool_wait_for_new_messages",
                {"since_rowid": 10**9, "timeout_seconds": 1.0},
            )
        )
        # Clock from before yielding: a blocked loop delays this sleep too.
        started = time.perf_counter()
        await asyncio.sleep(0.2)  # the wait is under way
        await server.mcp.call_tool("tool_check_db_access", {})
        elapsed = time.perf_counter() - started
        await waiting
        return elapsed

    elapsed = asyncio.run(scenario())
    assert elapsed < 0.6, f"check_db_access waited {elapsed:.2f}s behind the poll"


def test_a_slow_send_does_not_stall_other_calls(chat_db):
    def slow_send(**_: object) -> str:
        time.sleep(1.0)  # AppleScript talking to Messages.app
        return "Message sent"

    async def scenario() -> float:
        sending = asyncio.ensure_future(
            server.mcp.call_tool(
                "tool_send_message", {"recipient": ALICE, "message": "hi"}
            )
        )
        started = time.perf_counter()
        await asyncio.sleep(0.2)
        await server.mcp.call_tool("tool_check_db_access", {})
        elapsed = time.perf_counter() - started
        await sending
        return elapsed

    with patch("mac_messages_mcp.server.send_message", side_effect=slow_send):
        elapsed = asyncio.run(scenario())
    assert elapsed < 0.6, f"check_db_access waited {elapsed:.2f}s behind the send"
