"""Postgres-backed job queue.

There is no separate queue table. A submission row in ``QUEUED`` *is* the queue
entry, which removes the failure mode a broker introduces: with Celery, the row
was committed and the job enqueued as two separate operations, so a crash in
the gap left work that existed but would never run. Here the INSERT and the
enqueue are the same write, and either both happen or neither does.

Claiming uses ``FOR UPDATE SKIP LOCKED``: concurrent workers each take a
different row instead of blocking on the same one, so scaling out is just
running the process again. The claim is then a conditional UPDATE, which is
what keeps a claim exactly-once even if two workers race on the same id.

Sweeps (session expiry, the stuck-job reaper, idle-room closing) run on a timer
inside the same process, guarded by a Postgres advisory lock so that N workers
still produce one sweep rather than N.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from crucible.db.enums import SubmissionStatus
from crucible.db.models import Submission

# Distinct 64-bit keys for pg_try_advisory_lock. Arbitrary but fixed: changing
# one lets an old and a new worker sweep concurrently during a rolling restart.
SWEEP_LOCK_KEY = 0x0C121B1E5117E1


def claim_next_submission(db: Session, worker_id: str) -> uuid.UUID | None:
    """Take ownership of one queued submission, or return None if idle.

    SKIP LOCKED is the important part. Without it, every idle worker queues up
    behind the same row and throughput collapses to one worker's worth.
    """
    candidate = db.execute(
        select(Submission.id)
        .where(Submission.status == SubmissionStatus.QUEUED)
        .order_by(Submission.enqueued_at.asc().nulls_last(), Submission.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()

    if candidate is None:
        return None

    # Conditional UPDATE rather than a plain write: the row lock above is
    # released at commit, so this is what makes a claim safe against a
    # re-delivery or a reaper requeue racing the same id.
    claimed = db.execute(
        update(Submission)
        .where(Submission.id == candidate, Submission.status == SubmissionStatus.QUEUED)
        .values(
            status=SubmissionStatus.RUNNING,
            started_at=datetime.now(UTC),
            worker_id=worker_id,
            attempt=Submission.attempt + 1,
        )
        .returning(Submission.id)
    ).scalar_one_or_none()

    return claimed


def queue_depth(db: Session) -> int:
    """Submissions waiting for a worker."""
    return int(
        db.execute(
            select(func.count())
            .select_from(Submission)
            .where(Submission.status == SubmissionStatus.QUEUED)
        ).scalar_one()
    )


def try_sweep_lock(db: Session) -> bool:
    """Session-level advisory lock, so only one worker sweeps.

    Returns immediately rather than waiting: a worker that loses the race has
    nothing to do, because the winner is already running the same sweep.
    """
    return bool(
        db.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": SWEEP_LOCK_KEY}).scalar_one()
    )


def release_sweep_lock(db: Session) -> None:
    db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": SWEEP_LOCK_KEY})
