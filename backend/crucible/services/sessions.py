"""Timed, proctored practice tests.

The rule that governs this module: **the server owns the clock**. Every write
is checked against `ends_at` on the row, never against a duration the client
sends. A browser countdown is a display of that deadline, not a source of it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from crucible.core.logging import get_logger
from crucible.db.enums import (
    QuestionType,
    SessionStatus,
    SubmissionStatus,
    ViolationAction,
    ViolationKind,
)
from crucible.db.models import (
    Question,
    SessionItem,
    Submission,
    TestSession,
    User,
    Violation,
)
from crucible.schemas.common import Page
from crucible.schemas.question import QuestionDetail
from crucible.schemas.session import (
    DraftSave,
    SessionCreate,
    SessionDetail,
    SessionItemOut,
    SessionResult,
    SessionSummary,
    ViolationOut,
)
from crucible.services.questions import to_detail

log = get_logger(__name__)

# Warn on the first two violations, auto-submit on the third. A single
# accidental tab switch should not end someone's test; a pattern should.
VIOLATION_LIMIT = 3


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "session_not_found", "message": "No such session"},
    )


def _closed(session: TestSession) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "session_closed",
            "message": f"This session is {session.status.value} and can no longer be changed",
        },
    )


def _eager_items():
    """Load session -> items -> question -> test_cases in one statement.

    The chain has to reach test_cases, not stop at question. to_detail() reads
    question.test_cases to pick out the sample ones, and under asyncpg a lazy
    load at that point raises MissingGreenlet rather than quietly issuing
    another query, because serialisation runs outside the async context.
    """
    return (
        selectinload(TestSession.items)
        .selectinload(SessionItem.question)
        .selectinload(Question.test_cases)
    )


async def _load(db: AsyncSession, user: User, session_id: uuid.UUID) -> TestSession:
    """Load a session with everything any caller might touch.

    There was a with_items=False option here to skip the join for endpoints
    that "only" needed the session row. It was a trap: record_violation used
    it, then the third violation auto-submits, which walks session.items, and
    the lazy load raised MissingGreenlet under asyncpg.

    A session holds at most 25 items, so the join costs nothing worth
    optimising and the flag only created a way to be wrong.
    """
    session = (
        await db.execute(
            select(TestSession).where(TestSession.id == session_id).options(_eager_items())
        )
    ).scalar_one_or_none()

    # 404 rather than 403 for someone else's session: 403 would confirm the id
    # is real.
    if session is None or session.user_id != user.id:
        raise _not_found()
    return session


def _expire_if_due(session: TestSession) -> bool:
    """Close a session whose deadline has passed.

    Checked on every read as well as by the periodic sweeper. The sweeper runs
    every 30 seconds, and a candidate refreshing at second 29 must not still be
    able to write an answer.
    """
    if session.status is not SessionStatus.ACTIVE:
        return False
    if datetime.now(UTC) < session.ends_at:
        return False
    session.status = SessionStatus.AUTO_SUBMITTED
    session.submitted_at = session.ends_at
    return True


def _seconds_remaining(session: TestSession) -> int:
    if session.status is not SessionStatus.ACTIVE:
        return 0
    return max(0, int((session.ends_at - datetime.now(UTC)).total_seconds()))


async def start_session(db: AsyncSession, user: User, payload: SessionCreate) -> SessionDetail:
    """Create a session and lock in its question set."""
    # One active session at a time. Two open tests let someone read the
    # questions in one while the other's clock is paused on their screen.
    existing = (
        await db.execute(
            select(TestSession).where(
                TestSession.user_id == user.id,
                TestSession.status == SessionStatus.ACTIVE,
                TestSession.ends_at > datetime.now(UTC),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "session_already_active",
                "message": "Finish or abandon your current test first",
                "field": str(existing.id),
            },
        )

    questions = await _pick_questions(db, user, payload)
    if not questions:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "no_questions_available",
                "message": "No questions match those filters",
            },
        )

    now = datetime.now(UTC)
    session = TestSession(
        user_id=user.id,
        topics=payload.topics,
        question_types=[t.value for t in payload.question_types],
        target_difficulty=payload.difficulty,
        is_adaptive=payload.adaptive,
        duration_seconds=payload.duration_minutes * 60,
        starts_at=now,
        ends_at=now + timedelta(minutes=payload.duration_minutes),
        max_score=100.0 * len(questions),
    )
    for ordinal, question in enumerate(questions):
        session.items.append(
            SessionItem(
                question_id=question.id,
                ordinal=ordinal,
                draft_language=(question.allowed_languages or [None])[0],
                draft_code=(question.starter_code or {}).get(
                    (question.allowed_languages or [""])[0], ""
                ),
            )
        )

    db.add(session)
    await db.flush()

    # Re-read through the eager loader rather than refreshing. refresh(["items"])
    # populates items but leaves each item's question unloaded, and the lazy
    # load then fails during serialisation.
    session = (
        await db.execute(
            select(TestSession).where(TestSession.id == session.id).options(_eager_items())
        )
    ).scalar_one()

    log.info(
        "session.started",
        session_id=str(session.id),
        questions=len(questions),
        duration_minutes=payload.duration_minutes,
        adaptive=payload.adaptive,
    )
    return await _to_detail(db, session)


async def _pick_questions(db: AsyncSession, user: User, payload: SessionCreate) -> list[Question]:
    stmt = select(Question).where(Question.is_active.is_(True))
    if payload.topics:
        stmt = stmt.where(Question.topic.in_(payload.topics))
    if payload.question_types:
        stmt = stmt.where(Question.type.in_(payload.question_types))
    if payload.difficulty:
        stmt = stmt.where(Question.difficulty == payload.difficulty)

    # Random order so retaking the same filters does not serve the same test.
    stmt = stmt.order_by(func.random()).limit(payload.question_count)
    return list((await db.execute(stmt)).scalars().all())


async def _to_detail(db: AsyncSession, session: TestSession) -> SessionDetail:
    detail = SessionDetail.model_validate(session)
    detail.seconds_remaining = _seconds_remaining(session)

    items: list[SessionItemOut] = []
    for item in sorted(session.items, key=lambda i: i.ordinal):
        out = SessionItemOut.model_validate(item)
        if item.question is not None:
            # to_detail() is what strips the answer key. Serialising the
            # question any other way is how hidden test cases leak.
            out.question = QuestionDetail.model_validate(to_detail(item.question))
        items.append(out)
    detail.items = items
    return detail


async def get_session(db: AsyncSession, user: User, session_id: uuid.UUID) -> SessionDetail:
    session = await _load(db, user, session_id)
    if _expire_if_due(session):
        await _finalise(db, session)
    return await _to_detail(db, session)


async def save_draft(
    db: AsyncSession,
    user: User,
    session_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: DraftSave,
) -> None:
    """Autosave. Deliberately cheap and deliberately not a submission."""
    session = await _load(db, user, session_id)
    if _expire_if_due(session):
        await _finalise(db, session)
    if session.status is not SessionStatus.ACTIVE:
        raise _closed(session)

    item = next((i for i in session.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "item_not_found", "message": "No such question in this session"},
        )

    if payload.language is not None:
        item.draft_language = payload.language
    item.draft_code = payload.code
    item.draft_answer = payload.answer
    item.last_saved_at = datetime.now(UTC)


async def record_violation(
    db: AsyncSession,
    user: User,
    session_id: uuid.UUID,
    kind: ViolationKind,
    detail: dict,
    request_id: str | None = None,
) -> ViolationOut:
    """Log a proctoring event and act on it if the count is high enough.

    These are client-reported, so a determined cheat can suppress them. This
    raises the cost of casual cheating; it does not stop a motivated one. Real
    proctoring needs a lockdown browser, which is out of scope. The audit trail
    is append-only so a disputed result can be reviewed.
    """
    session = await _load(db, user, session_id)
    if session.status is not SessionStatus.ACTIVE:
        raise _closed(session)

    session.violation_count += 1
    action = (
        ViolationAction.AUTO_SUBMITTED
        if session.violation_count >= VIOLATION_LIMIT
        else ViolationAction.WARNED
    )

    record = Violation(
        session_id=session.id,
        user_id=user.id,
        kind=kind,
        action=action,
        running_count=session.violation_count,
        detail=detail,
        request_id=request_id,
    )
    db.add(record)

    if action is ViolationAction.AUTO_SUBMITTED:
        session.status = SessionStatus.AUTO_SUBMITTED
        session.submitted_at = datetime.now(UTC)
        await db.flush()
        await _finalise(db, session)
        log.warning(
            "session.auto_submitted",
            session_id=str(session.id),
            violations=session.violation_count,
            kind=kind.value,
        )

    await db.flush()
    return ViolationOut.model_validate(record)


async def submit_session(db: AsyncSession, user: User, session_id: uuid.UUID) -> SessionResult:
    """Finish a session and grade whatever was drafted.

    Every draft becomes a submission, so an answer typed but never explicitly
    run still counts. Losing someone's work because they did not press a second
    button is indefensible.
    """
    session = await _load(db, user, session_id)
    _expire_if_due(session)
    if session.status not in (SessionStatus.ACTIVE, SessionStatus.AUTO_SUBMITTED):
        raise _closed(session)

    if session.status is SessionStatus.ACTIVE:
        session.status = SessionStatus.SUBMITTED
        session.submitted_at = datetime.now(UTC)

    await _finalise(db, session)
    return await build_result(db, session)


async def _finalise(db: AsyncSession, session: TestSession) -> None:
    """Turn each drafted answer into a queued submission, exactly once."""
    queued: list[uuid.UUID] = []

    for item in session.items:
        if item.final_submission_id is not None:
            continue  # already finalised; safe to call this twice
        if not item.draft_code.strip() and not item.draft_answer:
            continue  # unattempted

        question = item.question
        submission = Submission(
            user_id=session.user_id,
            question_id=item.question_id,
            session_id=session.id,
            type=question.type if question else QuestionType.CODE,
            language=item.draft_language,
            source_code=item.draft_code,
            answer=item.draft_answer,
            status=SubmissionStatus.QUEUED,
            enqueued_at=datetime.now(UTC),
        )
        db.add(submission)
        await db.flush()
        item.final_submission_id = submission.id
        queued.append(submission.id)

    # Writing the rows in QUEUED is the enqueue; workers poll for that status.
    if queued:
        log.info("session.finalised", session_id=str(session.id), queued=len(queued))


async def build_result(db: AsyncSession, session: TestSession) -> SessionResult:
    """Score a finished session.

    Reads scores from the submissions rather than the item rows, because
    grading is asynchronous: immediately after submitting, most submissions are
    still queued and score zero. The result is therefore a live view that fills
    in as workers finish, not a snapshot taken at submit time.
    """
    rows = (
        await db.execute(
            select(Submission, Question.topic)
            .join(Question, Question.id == Submission.question_id)
            .where(Submission.session_id == session.id)
        )
    ).all()

    per_topic: dict[str, dict[str, float]] = {}
    total = 0.0
    for submission, topic in rows:
        bucket = per_topic.setdefault(topic, {"score": 0.0, "max": 0.0, "count": 0.0})
        bucket["score"] += submission.score
        bucket["max"] += submission.max_score
        bucket["count"] += 1
        total += submission.score

    for bucket in per_topic.values():
        bucket["percentage"] = round(
            100.0 * bucket["score"] / bucket["max"] if bucket["max"] else 0.0, 1
        )

    session.total_score = round(total, 2)
    max_score = session.max_score or 1.0

    # Only topics with at least one attempt, weakest first. A topic the
    # candidate never saw is not a weakness.
    weakest = sorted(
        (t for t, b in per_topic.items() if b["count"]),
        key=lambda t: per_topic[t]["percentage"],
    )[:3]

    return SessionResult(
        session_id=session.id,
        status=session.status,
        total_score=session.total_score,
        max_score=session.max_score,
        percentage=round(100.0 * total / max_score, 1),
        questions_attempted=len(rows),
        questions_total=len(session.items),
        violation_count=session.violation_count,
        per_topic=per_topic,
        weakest_topics=weakest,
    )


async def list_sessions(
    db: AsyncSession, user: User, *, limit: int = 20, offset: int = 0
) -> Page[SessionSummary]:
    rows = (
        (
            await db.execute(
                select(TestSession)
                .where(TestSession.user_id == user.id)
                .order_by(TestSession.created_at.desc())
                .limit(limit + 1)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return Page[SessionSummary](
        items=[SessionSummary.model_validate(s) for s in rows[:limit]],
        limit=limit,
        offset=offset,
        has_more=len(rows) > limit,
    )
