# Copyright (c) 2023 Carter Lasalle
"""Scheduled sends survive a server restart.

The queue used to live only in memory, so restarting the MCP server (or the
Claude app that launches it) silently dropped every pending message. Jobs are
now written to a private JSON file and reloaded on start, with a grace window
for jobs that came due while the server was down.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from mac_messages_mcp import scheduler as scheduler_module
from mac_messages_mcp.scheduler import MessageScheduler


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def store(tmp_path):
    return tmp_path / "state" / "scheduled_messages.json"


def _sender(log):
    def send(job):
        log.append(job.id)
        return f"sent {job.message}"

    return send


def test_jobs_survive_a_new_scheduler_instance(store):
    first = MessageScheduler(_sender([]), clock=_Clock(100.0), store_path=store)
    job = first.schedule(
        "+15555550100", "see you at 7", send_at=500.0, group_chat=False
    )
    assert job.id == "msg-1"

    second = MessageScheduler(_sender([]), clock=_Clock(101.0), store_path=store)
    (loaded,) = second.list()
    assert (
        loaded.id,
        loaded.recipient,
        loaded.message,
        loaded.send_at,
        loaded.status,
    ) == ("msg-1", "+15555550100", "see you at 7", 500.0, "pending")
    # Ids keep counting from where the file left off.
    assert second.schedule("x", "y", send_at=600.0).id == "msg-2"


def test_job_that_came_due_while_down_is_sent_on_restart_within_grace(store):
    MessageScheduler(_sender([]), clock=_Clock(100.0), store_path=store).schedule(
        "a", "late one", send_at=200.0
    )

    log: list[str] = []
    restarted = MessageScheduler(
        _sender(log),
        clock=_Clock(200.0 + 600),
        store_path=store,
        catch_up_grace_seconds=3600,
    )
    outcomes = restarted.run_due()
    assert log == ["msg-1"]
    assert outcomes[0][0].status == "sent"
    assert "late" in (outcomes[0][0].result or "")


def test_job_long_past_due_is_marked_missed_not_sent(store):
    MessageScheduler(_sender([]), clock=_Clock(100.0), store_path=store).schedule(
        "a", "too late", send_at=200.0
    )

    log: list[str] = []
    restarted = MessageScheduler(
        _sender(log),
        clock=_Clock(200.0 + 4000),
        store_path=store,
        catch_up_grace_seconds=3600,
    )
    restarted.run_due()
    (job,) = restarted.list()
    assert log == []
    assert job.status == "missed"
    assert "not running" in (job.result or "")


def test_job_interrupted_mid_send_is_not_resent(store):
    first = MessageScheduler(_sender([]), clock=_Clock(100.0), store_path=store)
    first.schedule("a", "m", send_at=100.0)
    # Simulate a crash after the job was claimed but before it finished.
    first._claim_due(100.0)
    assert first.list()[0].status == "running"

    log: list[str] = []
    restarted = MessageScheduler(_sender(log), clock=_Clock(150.0), store_path=store)
    restarted.run_due()
    (job,) = restarted.list()
    assert log == []
    assert job.status == "failed" and "interrupted" in (job.result or "")


def test_cancel_and_outcomes_persist(store):
    first = MessageScheduler(_sender([]), clock=_Clock(100.0), store_path=store)
    keep = first.schedule("a", "keep", send_at=900.0)
    drop = first.schedule("a", "drop", send_at=900.0)
    done = first.schedule("a", "done", send_at=100.0)
    assert first.cancel(drop.id)
    first.run_due()

    second = MessageScheduler(_sender([]), clock=_Clock(101.0), store_path=store)
    by_id = {j.id: j for j in second.list()}
    assert set(by_id) == {keep.id, done.id}
    assert by_id[done.id].status == "sent" and by_id[done.id].result == "sent done"


def test_store_is_private_and_written_atomically(store):
    sched = MessageScheduler(_sender([]), clock=_Clock(100.0), store_path=store)
    sched.schedule("a", "m", send_at=500.0)
    assert store.exists()
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert not list(store.parent.glob("*.tmp")), "no temp files left behind"
    data = json.loads(store.read_text())
    assert data["jobs"][0]["message"] == "m"


def test_corrupt_store_is_set_aside_not_fatal(store, caplog):
    store.parent.mkdir(parents=True)
    store.write_text("{not json")
    sched = MessageScheduler(_sender([]), clock=_Clock(100.0), store_path=store)
    assert sched.list() == []
    assert any(p.name.endswith(".corrupt") for p in store.parent.iterdir())
    assert sched.schedule("a", "m", send_at=500.0).id == "msg-1"


def test_default_store_path_and_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MAC_MESSAGES_SCHEDULE_STORE", raising=False)
    default = scheduler_module.default_store_path()
    assert (
        default
        == tmp_path
        / "Library"
        / "Application Support"
        / "mac-messages-mcp"
        / "scheduled_messages.json"
    )
    assert not default.exists(), "nothing is created until a job is scheduled"

    monkeypatch.setenv("MAC_MESSAGES_SCHEDULE_STORE", str(tmp_path / "custom.json"))
    assert scheduler_module.default_store_path() == tmp_path / "custom.json"

    monkeypatch.setenv("MAC_MESSAGES_SCHEDULE_STORE", "none")
    assert scheduler_module.default_store_path() is None


def test_server_scheduler_persists_and_lists_missed_jobs(monkeypatch, tmp_path):
    import importlib

    from mac_messages_mcp import server

    monkeypatch.setenv("MAC_MESSAGES_SCHEDULE_STORE", str(tmp_path / "s.json"))
    reloaded = importlib.reload(server)
    try:
        assert reloaded.scheduler.store_path == tmp_path / "s.json"
        out = reloaded.tool_schedule_message(
            "+15555550100", "hello", "2999-01-01T09:00:00"
        )
        assert "msg-1" in out and "survives a restart" in out
        assert (
            json.loads((tmp_path / "s.json").read_text())["jobs"][0]["recipient"]
            == "+15555550100"
        )
        listing = reloaded.tool_list_scheduled_messages()
        assert "[pending]" in listing
    finally:
        reloaded.scheduler.stop()
        monkeypatch.delenv("MAC_MESSAGES_SCHEDULE_STORE")
        importlib.reload(server)
