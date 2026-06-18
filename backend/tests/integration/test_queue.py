"""The Postgres-backed job queue, against a real database.

These need real Postgres because the guarantees under test are Postgres
guarantees: FOR UPDATE SKIP LOCKED handing concurrent workers different rows,
and a conditional UPDATE making a claim exactly-once. A mock cannot fail these
tests in the way a real database can.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from crucible.db.enums import QuestionType, SubmissionStatus
from crucible.db.models import Question, Submission, SubmissionResult, User
from crucible.db.session import sync_session_scope
from crucible.workers.queue import (
    claim_next_submission,
    queue_depth,
    release_sweep_lock,
    try_sweep_lock,
)

pytestmark = [pytest.mark.integration]


def _seed(db, *, count: int) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """Insert `count` queued submissions for a throwaway user."""
    question = db.execute(
        select(Question).where(Question.type == QuestionType.CODE).limit(1)
    ).scalar_one_or_none()
    if question is None:
        pytest.skip("question bank is empty -- run: make seed")

    user = User(
        email=f"queue-itest-{uuid.uuid4().hex[:12]}@crucible-itest.dev",
        display_name="Queue Test",
        password_hash="x",
    )
    db.add(user)
    db.flush()

    ids = []
    for i in range(count):
        submission = Submission(
            user_id=user.id,
            question_id=question.id,
            type=question.type,
            language="python",
            source_code=f"print({i})",
            status=SubmissionStatus.QUEUED,
            enqueued_at=datetime.now(UTC),
        )
        db.add(submission)
        db.flush()
        ids.append(submission.id)
    return user.id, ids


def _cleanup(user_id: uuid.UUID) -> None:
    with sync_session_scope() as db:
        subs = (
            db.execute(select(Submission.id).where(Submission.user_id == user_id)).scalars().all()
        )
        db.execute(delete(SubmissionResult).where(SubmissionResult.submission_id.in_(subs)))
        db.execute(delete(Submission).where(Submission.user_id == user_id))
        db.execute(delete(User).where(User.id == user_id))


def test_claim_marks_the_row_running_and_counts_the_attempt():
    with sync_session_scope() as db:
        user_id, ids = _seed(db, count=1)
    try:
        with sync_session_scope() as db:
            claimed = claim_next_submission(db, "worker-a")
        assert claimed in ids

        with sync_session_scope() as db:
            row = db.get(Submission, claimed)
            assert row.status is SubmissionStatus.RUNNING
            assert row.worker_id == "worker-a"
            # The attempt counter is what bounds retries, so the claim must
            # be the thing that increments it.
            assert row.attempt == 1
            assert row.started_at is not None
    finally:
        _cleanup(user_id)


def test_sequential_claims_hand_out_different_rows():
    """A claimed row leaves the queue, so the next claim moves on.

    This is the regression guard for claiming twice: if the claim did not
    actually take the row out of QUEUED, every worker would keep re-claiming
    the same submission.
    """
    with sync_session_scope() as db:
        user_id, ids = _seed(db, count=4)
    try:
        claimed = []
        for name in ("w1", "w2", "w3", "w4"):
            with sync_session_scope() as db:
                claimed.append(claim_next_submission(db, name))

        assert None not in claimed, "every queued row should be claimable"
        assert len(set(claimed)) == 4, "a row was handed out twice"
        assert set(claimed) == set(ids)
    finally:
        _cleanup(user_id)


def test_a_locked_row_does_not_block_another_worker():
    """The actual SKIP LOCKED guarantee, which needs overlapping transactions.

    Worker A holds its transaction open over a claimed row. Worker B must get
    a *different* row immediately. Without SKIP LOCKED, B blocks on A's lock
    until A commits, and one slow sandbox run stalls the whole fleet -- so the
    failure this catches is a throughput collapse, not a wrong answer.
    """
    import threading
    import time

    from crucible.db.session import get_sync_session_factory

    hold_seconds = 4.0

    with sync_session_scope() as db:
        user_id, ids = _seed(db, count=2)

    a_claimed: list[uuid.UUID | None] = []
    b_claimed: list[uuid.UUID | None] = []
    b_elapsed: list[float] = []
    a_has_locked = threading.Event()

    def worker_a() -> None:
        session = get_sync_session_factory()()
        try:
            a_claimed.append(claim_next_submission(session, "hold-a"))
            a_has_locked.set()
            # Hold the row lock open, unconditionally, for longer than B's
            # budget. B must not need A to finish.
            time.sleep(hold_seconds)
            session.commit()
        finally:
            session.close()

    def worker_b() -> None:
        a_has_locked.wait(timeout=10)
        started = time.monotonic()
        with sync_session_scope() as session:
            b_claimed.append(claim_next_submission(session, "hold-b"))
        b_elapsed.append(time.monotonic() - started)

    try:
        threads = [threading.Thread(target=worker_a), threading.Thread(target=worker_b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert a_claimed and b_claimed, "both workers should have claimed something"
        assert a_claimed[0] != b_claimed[0], "both workers claimed the same row"
        assert {a_claimed[0], b_claimed[0]} == set(ids)
        # The real assertion. Waiting out A's lock also ends with two different
        # rows, so only the timing distinguishes skipping from blocking.
        assert b_elapsed[0] < hold_seconds / 2, (
            f"worker B took {b_elapsed[0]:.1f}s while A held its lock for "
            f"{hold_seconds}s -- it blocked instead of skipping"
        )
    finally:
        _cleanup(user_id)


def test_claiming_is_fifo_by_enqueue_time():
    with sync_session_scope() as db:
        user_id, ids = _seed(db, count=3)
    try:
        with sync_session_scope() as db:
            first = claim_next_submission(db, "w1")
        assert first == ids[0], "the oldest queued submission should go first"
    finally:
        _cleanup(user_id)


def test_claiming_past_the_end_of_the_queue_returns_none():
    """An idle worker must get None, not an exception and not a stale row."""
    with sync_session_scope() as db:
        user_id, ids = _seed(db, count=1)
    try:
        # Drain everything currently queued, so the next claim has nothing
        # left to find regardless of what other rows exist in this database.
        drained = []
        with sync_session_scope() as db:
            while (claimed := claim_next_submission(db, "drainer")) is not None:
                drained.append(claimed)

        assert set(ids) <= set(drained), "our seeded row should have been drained"

        with sync_session_scope() as db:
            assert claim_next_submission(db, "idle-worker") is None
            assert queue_depth(db) == 0
    finally:
        _cleanup(user_id)


def test_queue_depth_tracks_claims():
    with sync_session_scope() as db:
        user_id, _ = _seed(db, count=2)
    try:
        with sync_session_scope() as db:
            before = queue_depth(db)
        with sync_session_scope() as db:
            claim_next_submission(db, "w1")
        with sync_session_scope() as db:
            assert queue_depth(db) == before - 1
    finally:
        _cleanup(user_id)


def test_only_one_worker_holds_the_sweep_lock():
    """Otherwise N workers run N copies of every sweep."""
    with sync_session_scope() as holder:
        assert try_sweep_lock(holder) is True

        # A second session must be refused while the first holds it.
        with sync_session_scope() as contender:
            assert try_sweep_lock(contender) is False

        release_sweep_lock(holder)

    # Once released, it is available again.
    with sync_session_scope() as db:
        assert try_sweep_lock(db) is True
        release_sweep_lock(db)
