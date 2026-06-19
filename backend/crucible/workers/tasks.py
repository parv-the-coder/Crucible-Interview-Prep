"""Background jobs.

The core one is evaluate_submission. Everything about how it handles failure is
deliberate; see docs/07-async-evaluation.md for the reasoning in full.

These are plain functions, not broker tasks. The worker in runner.py claims a
submission from Postgres and calls evaluate_submission directly, so there is no
serialisation boundary and no second place for work to be lost. Chained work
(ratings, AI review) runs after the grading transaction has committed, each in
its own transaction, so a failure there cannot roll back a correct grade.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from crucible.core.config import settings
from crucible.core.logging import get_logger
from crucible.db.enums import SessionStatus, SubmissionStatus
from crucible.db.models import (
    InterviewRoom,
    Question,
    Submission,
    SubmissionResult,
    TestSession,
)
from crucible.db.session import sync_session_scope
from crucible.evaluation.strategies import (
    EvaluationContext,
    TestCaseSpec,
    UnsupportedQuestionTypeError,
    build_strategy,
)

log = get_logger(__name__)


def evaluate_submission(submission_id: str, *, worker_id: str = "inline") -> dict[str, object]:
    """Grade one submission.

    Safe to run more than once for the same id: the reaper can requeue a row
    whose worker died mid-run, so this must be idempotent.

    The runner claims a row before calling this (see workers/queue.py), so the
    normal path here is a verification, not a claim. Re-claiming would
    double-count `attempt` and burn the retry budget twice per run.
    """
    sid = uuid.UUID(submission_id)

    with sync_session_scope() as db:
        # --- verify the claim ------------------------------------------
        submission = db.get(Submission, sid)
        if submission is None:
            log.info("submission.claim_skipped", submission_id=submission_id, status="missing")
            return {"submission_id": submission_id, "skipped": True}

        if submission.status is SubmissionStatus.QUEUED:
            # A direct caller handed us an unclaimed row. Claim it with a
            # conditional UPDATE, not a read-then-write: Postgres serialises
            # concurrent updates to one row, so exactly one caller sees a
            # rowcount of 1 and no two workers can both run the same code.
            claimed = db.execute(
                update(Submission)
                .where(Submission.id == sid, Submission.status == SubmissionStatus.QUEUED)
                .values(
                    status=SubmissionStatus.RUNNING,
                    started_at=datetime.now(UTC),
                    worker_id=worker_id,
                    attempt=Submission.attempt + 1,
                )
                .returning(Submission.id)
            ).scalar_one_or_none()
            if claimed is None:
                log.info(
                    "submission.claim_skipped", submission_id=submission_id, status="lost_race"
                )
                return {"submission_id": submission_id, "skipped": True}
            db.refresh(submission)

        elif submission.status is not SubmissionStatus.RUNNING or submission.worker_id != worker_id:
            # Already finished, or the reaper handed it to someone else.
            log.info(
                "submission.claim_skipped",
                submission_id=submission_id,
                status=submission.status.value,
            )
            return {"submission_id": submission_id, "skipped": True}

        if submission.enqueued_at and submission.started_at:
            wait_s = (submission.started_at - submission.enqueued_at).total_seconds()
            submission.queue_wait_ms = max(0, int(wait_s * 1000))

        question = db.execute(
            select(Question)
            .where(Question.id == submission.question_id)
            .options(selectinload(Question.test_cases))
        ).scalar_one_or_none()

        if question is None:
            _fail(db, submission, "Question no longer exists")
            return {"submission_id": submission_id, "status": "failed"}

        ctx = EvaluationContext(
            submission_id=submission.id,
            question_id=question.id,
            question_type=question.type.value,
            language=submission.language,
            source_code=submission.source_code,
            answer=submission.answer or {},
            payload=question.payload or {},
            test_cases=tuple(
                TestCaseSpec(
                    id=tc.id,
                    ordinal=tc.ordinal,
                    stdin=tc.stdin,
                    expected_stdout=tc.expected_stdout,
                    is_sample=tc.is_sample,
                    weight=tc.weight,
                )
                for tc in question.test_cases
            ),
            time_limit_ms=question.time_limit_ms,
            memory_limit_mb=question.memory_limit_mb,
            is_dry_run=submission.is_dry_run,
        )

    # --- evaluate (outside the transaction) -----------------------------
    # Sandbox execution can take ten seconds per case. Holding a database
    # transaction open across it would pin a connection and, worse, hold row
    # locks while running untrusted code.
    try:
        strategy = build_strategy(ctx.question_type)
        result = strategy.evaluate(ctx)
    except UnsupportedQuestionTypeError as exc:
        with sync_session_scope() as db:
            _fail(db, db.get(Submission, sid), str(exc))
        return {"submission_id": submission_id, "status": "failed"}
    except Exception as exc:
        # Infrastructure failure (daemon down, DB blip), not bad user code.
        # Returning the row to QUEUED *is* the retry -- the next poll re-claims
        # it. `attempt` was incremented by that claim, so it bounds the number
        # of retries without needing a separate counter, and a permanently
        # broken sandbox fails the submission after submission_max_attempts
        # rather than spinning forever.
        log.exception("submission.evaluation_error", submission_id=submission_id, error=str(exc))
        with sync_session_scope() as db:
            sub = db.get(Submission, sid)
            if sub is None:
                return {"submission_id": submission_id, "status": "missing"}
            if sub.attempt < settings.submission_max_attempts:
                sub.status = SubmissionStatus.QUEUED
                sub.worker_id = None
                sub.started_at = None
                return {"submission_id": submission_id, "status": "requeued"}
            _fail(db, sub, f"Evaluation failed after retries: {exc}")
        return {"submission_id": submission_id, "status": "failed"}

    # --- persist --------------------------------------------------------
    with sync_session_scope() as db:
        submission = db.get(Submission, sid)
        if submission is None:
            return {"submission_id": submission_id, "status": "vanished"}

        submission.status = SubmissionStatus.COMPLETED
        submission.finished_at = datetime.now(UTC)
        submission.score = result.score
        submission.max_score = result.max_score
        submission.passed = result.passed
        submission.cases_passed = result.cases_passed
        submission.cases_total = result.cases_total
        submission.compile_output = result.compile_output
        submission.error_message = result.error_message
        submission.execution_ms = result.execution_ms
        submission.peak_memory_kb = result.peak_memory_kb

        # Replace rather than append: a retry must not leave two generations
        # of results attached to one submission.
        db.query(SubmissionResult).filter(SubmissionResult.submission_id == sid).delete()
        for case in result.cases:
            db.add(
                SubmissionResult(
                    submission_id=sid,
                    test_case_id=case.test_case_id,
                    ordinal=case.ordinal,
                    outcome=case.outcome,
                    execution_ms=case.execution_ms,
                    memory_kb=case.memory_kb,
                    exit_code=case.exit_code,
                    stdout=case.stdout,
                    stderr=case.stderr,
                    is_visible=case.is_visible,
                )
            )

        score = submission.score
        passed = submission.passed

    log.info(
        "submission.completed",
        submission_id=submission_id,
        score=score,
        passed=passed,
        cases=f"{result.cases_passed}/{result.cases_total}",
        execution_ms=result.execution_ms,
    )
    return {
        "submission_id": submission_id,
        "status": "completed",
        "score": score,
        "passed": passed,
    }


def _fail(db, submission: Submission | None, message: str) -> None:
    if submission is None:
        return
    submission.status = SubmissionStatus.FAILED
    submission.finished_at = datetime.now(UTC)
    submission.error_message = message[:2000]


# --------------------------------------------------------------- sweepers ---


def reap_stuck_submissions() -> dict[str, int]:
    """Requeue submissions whose worker died mid-evaluation.

    task_reject_on_worker_lost handles a clean loss, but a worker that is
    OOM-killed or whose host disappears leaves rows in RUNNING forever. This is
    the backstop, and it is why submissions carry started_at and attempt.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.sandbox_timeout_seconds * 10 + 180)
    requeued = 0
    abandoned = 0

    with sync_session_scope() as db:
        stuck = (
            db.execute(
                select(Submission).where(
                    Submission.status == SubmissionStatus.RUNNING,
                    Submission.started_at < cutoff,
                )
            )
            .scalars()
            .all()
        )
        for submission in stuck:
            if submission.attempt >= settings.submission_max_attempts:
                _fail(db, submission, "Abandoned: exceeded retry budget after worker loss")
                abandoned += 1
            else:
                submission.status = SubmissionStatus.QUEUED
                submission.worker_id = None
                submission.started_at = None
                requeued += 1

    # No re-enqueue call: setting status back to QUEUED *is* the enqueue, and
    # the next worker poll picks the row up.

    if requeued or abandoned:
        log.warning("submissions.reaped", requeued=requeued, abandoned=abandoned)
    return {"requeued": requeued, "abandoned": abandoned}


def expire_sessions() -> dict[str, int]:
    """Close timed sessions past their deadline.

    The server owns the clock. A candidate who closes the browser at minute 29
    of a 30-minute test still gets their session finalised.
    """
    now = datetime.now(UTC)
    with sync_session_scope() as db:
        expired = db.execute(
            update(TestSession)
            .where(TestSession.status == SessionStatus.ACTIVE, TestSession.ends_at < now)
            .values(status=SessionStatus.AUTO_SUBMITTED, submitted_at=now)
            .returning(TestSession.id)
        ).all()
    count = len(expired)
    if count:
        log.info("sessions.auto_submitted", count=count)
    return {"expired": count}


def close_idle_rooms() -> dict[str, int]:
    """End interview rooms nobody has touched in a while."""
    from crucible.db.enums import RoomStatus

    cutoff = datetime.now(UTC) - timedelta(seconds=settings.room_idle_timeout_seconds)
    with sync_session_scope() as db:
        closed = db.execute(
            update(InterviewRoom)
            .where(
                InterviewRoom.status != RoomStatus.ENDED,
                InterviewRoom.last_activity_at < cutoff,
            )
            .values(status=RoomStatus.ENDED, ended_at=datetime.now(UTC))
            .returning(InterviewRoom.id)
        ).all()
    return {"closed": len(closed)}


__all__ = [
    "close_idle_rooms",
    "evaluate_submission",
    "expire_sessions",
    "reap_stuck_submissions",
]
