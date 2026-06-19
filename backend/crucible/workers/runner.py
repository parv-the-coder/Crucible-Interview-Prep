"""Worker process.

    python -m crucible.workers.runner

Polls Postgres for queued submissions, grades them, and runs the periodic
sweeps. Run it as many times as you want capacity: ``FOR UPDATE SKIP LOCKED``
means two workers never claim the same row, so scaling out needs no
coordination and no leader election.

Why polling rather than LISTEN/NOTIFY: a notification is fire-and-forget, so a
worker that is busy or restarting misses it and the row waits until something
else wakes it. Polling cannot miss work -- the queue is a table, and the query
is against a partial index -- and the cost of an idle poll is one indexed count
per second. The latency floor it adds is bounded by POLL_INTERVAL, which is
small next to the seconds a sandbox run takes.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import time
from datetime import UTC, datetime
from types import FrameType

from crucible.core.config import settings
from crucible.core.logging import configure_logging, get_logger
from crucible.db.session import sync_session_scope
from crucible.workers.queue import claim_next_submission, release_sweep_lock, try_sweep_lock
from crucible.workers.tasks import (
    close_idle_rooms,
    evaluate_submission,
    expire_sessions,
    reap_stuck_submissions,
)

log = get_logger(__name__)

# Sleep only when the queue is empty. A busy worker never waits.
POLL_INTERVAL_SECONDS = 1.0

# Sweep cadences, in seconds.
SWEEPS = (
    ("expire_sessions", expire_sessions, 30.0),
    ("reap_stuck_submissions", reap_stuck_submissions, 60.0),
    ("close_idle_rooms", close_idle_rooms, 120.0),
)


class Worker:
    def __init__(self) -> None:
        self.id = f"{socket.gethostname()}:{os.getpid()}"
        self._stopping = False
        self._last_swept: dict[str, float] = {}

    # ---------------------------------------------------------- lifecycle --

    def request_stop(self, signum: int, _frame: FrameType | None) -> None:
        """Finish the submission in hand, then exit.

        Killing mid-evaluation is survivable -- the reaper requeues the row --
        but it costs a candidate a re-run, so a clean signal drains instead.
        """
        if self._stopping:  # second signal: the operator means it
            log.warning("worker.force_quit", signal=signum)
            sys.exit(1)
        self._stopping = True
        log.info("worker.stopping", signal=signum, hint="finishing current job")

    def run(self) -> None:
        configure_logging()
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        self._warm_sandbox()
        log.info("worker.started", worker_id=self.id, sandbox=settings.sandbox_backend)

        try:
            while not self._stopping:
                worked = self._drain_one()
                self._run_due_sweeps()
                if not worked:
                    time.sleep(POLL_INTERVAL_SECONDS)
        finally:
            self._shutdown_sandbox()
            log.info("worker.stopped", worker_id=self.id)

    # ------------------------------------------------------------- sandbox --

    def _warm_sandbox(self) -> None:
        if settings.sandbox_backend != "docker":
            return
        try:
            from crucible.evaluation.sandbox import get_sandbox

            sandbox = get_sandbox()
            sandbox.warmup()
            log.info("worker.sandbox_ready", backend=sandbox.capabilities.name)
        except Exception as exc:
            log.error("worker.sandbox_warmup_failed", error=str(exc))

    def _shutdown_sandbox(self) -> None:
        """Destroy pooled containers.

        Without this a restart leaks the whole warm pool and the host
        accumulates containers until it runs out of memory.
        """
        try:
            from crucible.evaluation.sandbox import reset_sandbox

            reset_sandbox()
        except Exception as exc:
            log.warning("worker.sandbox_shutdown_failed", error=str(exc))

    # --------------------------------------------------------------- work --

    def _drain_one(self) -> bool:
        """Claim and grade a single submission. True if there was one."""
        try:
            with sync_session_scope() as db:
                claimed = claim_next_submission(db, self.id)
        except Exception as exc:
            # Almost always the database being briefly unreachable. Back off
            # rather than spinning on a failing connection.
            log.warning("worker.claim_failed", error=str(exc))
            time.sleep(POLL_INTERVAL_SECONDS)
            return False

        if claimed is None:
            return False

        try:
            evaluate_submission(str(claimed), worker_id=self.id)
        except Exception as exc:
            # evaluate_submission already marks the row failed for errors it
            # anticipates. Reaching here means something unforeseen, so leave
            # the row RUNNING and let the reaper decide: retry, or abandon once
            # the attempt budget is spent.
            log.exception("worker.evaluate_crashed", submission_id=str(claimed), error=str(exc))
        return True

    # ------------------------------------------------------------- sweeps --

    def _run_due_sweeps(self) -> None:
        now = time.monotonic()
        due = [
            (name, fn)
            for name, fn, every in SWEEPS
            if now - self._last_swept.get(name, 0.0) >= every
        ]
        if not due:
            return

        # One sweep across the fleet, not one per worker. The lock is held for
        # the sweep only; a worker that does not get it simply skips this pass.
        try:
            with sync_session_scope() as db:
                if not try_sweep_lock(db):
                    return
                try:
                    for name, fn in due:
                        self._last_swept[name] = now
                        self._invoke_sweep(name, fn)
                finally:
                    release_sweep_lock(db)
        except Exception as exc:
            log.warning("worker.sweep_lock_failed", error=str(exc))

    def _invoke_sweep(self, name: str, fn) -> None:
        try:
            result = fn()
        except Exception as exc:
            log.warning("worker.sweep_failed", sweep=name, error=str(exc))
            return
        # Only speak up when a sweep actually did something; a quiet system
        # should produce a quiet log.
        if any(result.values()):
            log.info("worker.sweep", sweep=name, **result, at=datetime.now(UTC).isoformat())


def main() -> None:
    Worker().run()


if __name__ == "__main__":
    main()
