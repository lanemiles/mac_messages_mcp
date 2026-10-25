# Copyright (c) 2023 Carter Lasalle
"""Scheduled-send registry for the Messages MCP server, persisted to disk.

Jobs are kept in memory and, when a ``store_path`` is given, mirrored to a
private JSON file after every change (atomic replace, mode 0600). A new
scheduler loads that file, so a restart of the server or of the client that
launches it does not lose pending sends. Jobs that came due while the server
was down are sent on the next run if they are within ``catch_up_grace_seconds``
of their send time, and marked ``missed`` otherwise; a job found ``running`` in
the file was interrupted mid-send and is marked ``failed`` rather than risking
a duplicate.

``MAC_MESSAGES_SCHEDULE_STORE`` overrides the default store location
(``~/Library/Application Support/mac-messages-mcp/scheduled_messages.json``);
``none`` disables persistence.
"""

from __future__ import annotations

import builtins
import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, fields
from pathlib import Path

__all__ = ["ScheduledMessage", "MessageScheduler", "default_store_path"]

logger = logging.getLogger(__name__)

STORE_ENV_VAR = "MAC_MESSAGES_SCHEDULE_STORE"
_DEFAULT_STORE = (
    "~/Library/Application Support/mac-messages-mcp/scheduled_messages.json"
)
# Finished jobs older than this are dropped from the file on the next save.
_TERMINAL_RETENTION_SECONDS = 7 * 24 * 3600.0
_DEFAULT_GRACE_SECONDS = 3600.0


def default_store_path() -> Path | None:
    """Where the server persists its queue; None when persistence is disabled."""
    raw = os.environ.get(STORE_ENV_VAR)
    if raw is not None and raw.strip().lower() in {"none", "off", ""}:
        return None
    return Path(raw if raw else _DEFAULT_STORE).expanduser()


# Ceiling on simultaneously pending jobs. Scheduling past this raises
# ValueError so an unbounded caller cannot grow memory without limit.
_MAX_PENDING_JOBS = 1000

# Longest single Condition.wait slice. The worker re-checks its stop flag at
# least this often, so stop() returns promptly even when the next job is far
# in the future.
_MAX_WAIT_SLICE_SECONDS = 0.25


@dataclass
class ScheduledMessage:
    """One scheduled send.

    Records are created by :meth:`MessageScheduler.schedule` and exist only
    for the lifetime of the server process; they are never persisted.
    """

    id: str
    recipient: str
    message: str
    send_at: float
    group_chat: bool = False
    attachment_paths: tuple[str, ...] = ()
    status: str = "pending"  # "pending" | "running" | "sent" | "failed" | "missed"
    result: str | None = None
    created_at: float = 0.0
    finished_at: float | None = None


class MessageScheduler:
    """Registry of in-memory scheduled sends.

    The scheduler is process-local by design: jobs exist only while the
    server process is alive and are never written to disk, so a restart loses
    every pending job. All public methods are safe to call from any thread and
    are guarded by a single condition variable over ``_jobs``.
    """

    def __init__(
        self,
        send: Callable[[ScheduledMessage], str],
        *,
        clock: Callable[[], float] = time.time,
        store_path: Path | None = None,
        catch_up_grace_seconds: float = _DEFAULT_GRACE_SECONDS,
    ) -> None:
        self._send = send
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._jobs: dict[str, ScheduledMessage] = {}
        self._counter = 0
        self._thread: threading.Thread | None = None
        self._stopping = False
        self.store_path = Path(store_path) if store_path is not None else None
        self.catch_up_grace_seconds = float(catch_up_grace_seconds)
        # Jobs restored from disk; a late send of one of these is annotated so
        # the listing shows the delivery was delayed by the downtime.
        self._restored_ids: set[str] = set()
        if self.store_path is not None:
            self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Read the store, reconciling jobs that aged while the server was down."""
        assert self.store_path is not None
        if not self.store_path.exists():
            return
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            raw_jobs = data["jobs"]
            if not isinstance(raw_jobs, list):
                raise ValueError("jobs is not a list")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            corrupt = self.store_path.with_suffix(self.store_path.suffix + ".corrupt")
            logger.warning(
                "Scheduled-message store %s is unreadable (%s); setting it aside as %s",
                self.store_path,
                exc,
                corrupt,
            )
            try:
                os.replace(self.store_path, corrupt)
            except OSError:
                pass
            return

        allowed = {f.name for f in fields(ScheduledMessage)}
        now = self._clock()
        for raw in raw_jobs:
            if not isinstance(raw, dict) or "id" not in raw:
                continue
            payload = {k: v for k, v in raw.items() if k in allowed}
            payload["attachment_paths"] = tuple(payload.get("attachment_paths") or ())
            try:
                job = ScheduledMessage(**payload)
            except TypeError:
                continue
            self._reconcile_loaded(job, now)
            self._jobs[job.id] = job
            if job.status == "pending":
                self._restored_ids.add(job.id)
            try:
                self._counter = max(self._counter, int(job.id.split("-", 1)[1]))
            except (IndexError, ValueError):
                pass

    def _reconcile_loaded(self, job: ScheduledMessage, now: float) -> None:
        if job.status == "running":
            # Claimed before a crash; whether Messages sent it is unknown, and a
            # duplicate is worse than a report, so do not retry.
            job.status = "failed"
            job.result = "interrupted: the server stopped while this job was sending"
            job.finished_at = now
        elif (
            job.status == "pending" and now - job.send_at > self.catch_up_grace_seconds
        ):
            late_minutes = int((now - job.send_at) // 60)
            job.status = "missed"
            job.result = (
                f"server was not running at send time; {late_minutes} minutes "
                f"past due exceeds the {int(self.catch_up_grace_seconds // 60)}"
                "-minute catch-up window"
            )
            job.finished_at = now

    def _save_locked(self) -> None:
        """Write the queue atomically; caller holds the condition."""
        if self.store_path is None:
            return
        now = self._clock()
        keep = [
            job
            for job in self._jobs.values()
            if job.status in ("pending", "running")
            or job.finished_at is None
            or now - job.finished_at <= _TERMINAL_RETENTION_SECONDS
        ]
        payload = {
            "version": 1,
            "jobs": [
                asdict(job) for job in sorted(keep, key=lambda j: (j.send_at, j.id))
            ],
        }
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.store_path)
        except OSError as exc:
            logger.warning(
                "Could not persist scheduled messages to %s: %s", self.store_path, exc
            )

    def schedule(
        self,
        recipient: str,
        message: str,
        send_at: float,
        *,
        group_chat: bool = False,
        attachment_paths: Iterable[str] = (),
    ) -> ScheduledMessage:
        """Register a send and return its job record.

        Ids are assigned from a monotonically increasing counter (``msg-1``,
        ``msg-2``, ...). A job needs a non-empty recipient, a finite positive
        ``send_at``, and either a non-empty message or at least one attachment
        path; otherwise ValueError is raised. More than
        ``_MAX_PENDING_JOBS`` pending jobs also raises ValueError.

        The returned job lives only in this process -- nothing is persisted.
        """
        attachments = tuple(attachment_paths)
        if not recipient:
            raise ValueError("recipient must be non-empty")
        if not message and not attachments:
            raise ValueError("message or at least one attachment path is required")
        if (
            not isinstance(send_at, (int, float))
            or not math.isfinite(send_at)
            or send_at <= 0
        ):
            raise ValueError("send_at must be a finite unix timestamp greater than 0")

        with self._condition:
            pending = sum(1 for job in self._jobs.values() if job.status == "pending")
            if pending >= _MAX_PENDING_JOBS:
                raise ValueError(
                    f"cannot schedule more than {_MAX_PENDING_JOBS} pending jobs"
                )
            self._counter += 1
            job = ScheduledMessage(
                id=f"msg-{self._counter}",
                recipient=recipient,
                message=message,
                send_at=float(send_at),
                group_chat=group_chat,
                attachment_paths=attachments,
                created_at=self._clock(),
            )
            self._jobs[job.id] = job
            self._save_locked()
            self._condition.notify_all()
            return job

    def list(self) -> builtins.list[ScheduledMessage]:
        """Return every known job in stable order by ``send_at`` then ``id``.

        Jobs that already ran stay listed with their terminal status; cancelled
        jobs are removed. The order is deterministic.
        """
        with self._condition:
            return sorted(self._jobs.values(), key=lambda job: (job.send_at, job.id))

    def cancel(self, job_id: str) -> bool:
        """Cancel a pending job and return whether one was removed.

        Unknown ids and jobs that already executed return False and leave the
        registry unchanged.
        """
        with self._condition:
            job = self._jobs.get(job_id)
            if job is None or job.status != "pending":
                return False
            del self._jobs[job_id]
            self._save_locked()
            self._condition.notify_all()
            return True

    def run_due(
        self, now: float | None = None
    ) -> builtins.list[tuple[ScheduledMessage, str]]:
        """Execute every pending job whose ``send_at`` is at or before ``now``.

        ``now`` defaults to the scheduler clock. Each executed job is marked
        ``"sent"`` with the callback's return value, or ``"failed"`` with the
        exception text when the callback raises; exceptions never propagate to
        the caller. Jobs that already ran are never executed again. Returns
        ``(job, outcome)`` pairs in ``send_at``/``id`` order.
        """
        moment = self._clock() if now is None else now
        return self._execute(self._claim_due(moment))

    def _claim_due(self, now: float) -> builtins.list[ScheduledMessage]:
        """Mark every pending job due at ``now`` as running and return them.

        Jobs are claimed under the lock and run outside it: a real send shells
        out to AppleScript and can take seconds, which must not block
        ``schedule``, ``cancel``, ``list``, or ``stop``.
        """
        with self._condition:
            due = [
                job
                for job in self._jobs.values()
                if job.status == "pending" and job.send_at <= now
            ]
            due.sort(key=lambda job: (job.send_at, job.id))
            for job in due:
                job.status = "running"
            if due:
                self._save_locked()
            self._condition.notify_all()
            return due

    def _execute(
        self,
        jobs: builtins.list[ScheduledMessage],
    ) -> builtins.list[tuple[ScheduledMessage, str]]:
        """Run claimed jobs and record their outcomes; never raises."""
        executed: builtins.list[tuple[ScheduledMessage, str]] = []
        for job in jobs:
            try:
                outcome = self._send(job)
            except Exception as exc:  # record failures; never let a send escape
                outcome = str(exc)
                failed = True
            else:
                failed = False
            finished = self._clock()
            lateness = finished - job.send_at
            if not failed and lateness > 120 and job.id in self._restored_ids:
                outcome = (
                    f"{outcome} (sent {int(lateness // 60)} minutes late: the "
                    "server was not running at send time)"
                )
            with self._condition:
                job.status = "failed" if failed else "sent"
                job.result = outcome
                job.finished_at = finished
                self._save_locked()
                self._condition.notify_all()
            executed.append((job, outcome))
        return executed

    def has_pending(self) -> bool:
        with self._condition:
            return any(job.status == "pending" for job in self._jobs.values())

    def start(self) -> None:
        """Start the background worker thread.

        The worker is a daemon so it cannot keep the process alive, and start()
        is idempotent: calling it while the worker runs is a no-op.
        """
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            thread = threading.Thread(
                target=self._run_loop,
                name="mac-messages-scheduler",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the worker thread, blocking at most ``timeout`` seconds.

        Idempotent and bounded: the worker wakes from its longest wait slice
        and exits, so this returns promptly even when the next job is far in
        the future.
        """
        with self._condition:
            self._stopping = True
            thread = self._thread
            self._condition.notify_all()
        if thread is None:
            return
        thread.join(timeout)
        if not thread.is_alive():
            with self._condition:
                if self._thread is thread:
                    self._thread = None

    def _next_due_locked(self) -> float | None:
        """Earliest pending ``send_at``, or None; caller holds the condition."""
        send_times = [
            job.send_at for job in self._jobs.values() if job.status == "pending"
        ]
        return min(send_times) if send_times else None

    def _run_loop(self) -> None:
        """Worker body: wait for the next due time, then execute it."""
        while True:
            with self._condition:
                if self._stopping:
                    return
                next_due = self._next_due_locked()
                if next_due is None:
                    self._condition.wait(_MAX_WAIT_SLICE_SECONDS)
                    continue
                now = self._clock()
                if next_due > now:
                    self._condition.wait(min(next_due - now, _MAX_WAIT_SLICE_SECONDS))
                    continue
            # Due: claim under the lock, then send outside it.
            self._execute(self._claim_due(self._clock()))
