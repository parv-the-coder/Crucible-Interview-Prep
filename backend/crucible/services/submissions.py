"""Submission intake.

The API's job here is deliberately small: validate, persist, enqueue, return
202. Everything expensive happens in a worker. See docs/07-async-evaluation.md.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from crucible.core.config import settings
from crucible.core.logging import get_logger
from crucible.db.enums import QuestionType, SubmissionStatus, UserRole
from crucible.db.models import AIInteraction, Question, Submission, User
from crucible.evaluation.sandbox import supported_languages
from crucible.schemas.common import Page
from crucible.schemas.submission import (
    ResultOut,
    SubmissionAccepted,
    SubmissionCreate,
    SubmissionDetail,
    SubmissionSummary,
)

log = get_logger(__name__)


def _bad_request(code: str, message: str, field: str | None = None) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": code, "message": message, "field": field},
    )


async def create_submission(
    db: AsyncSession,
    user: User,
    payload: SubmissionCreate,
    *,
    idempotency_key: str | None = None,
) -> SubmissionAccepted:
    # --- idempotency ----------------------------------------------------
    # Checked before anything else so a retried POST is cheap. The unique
    # index is still the real guarantee -- this check races, the index does
    # not, and the IntegrityError below closes the window.
    if idempotency_key:
        existing = (
            await db.execute(
                select(Submission).where(
                    Submission.user_id == user.id,
                    Submission.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _accepted(existing, deduplicated=True)

    question = (
        await db.execute(
            select(Question)
            .where(Question.id == payload.question_id)
            .options(selectinload(Question.test_cases))
        )
    ).scalar_one_or_none()

    if question is None or not question.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "question_not_found", "message": "No such question"},
        )

    # --- per-type validation --------------------------------------------
    if question.type is QuestionType.CODE:
        if not payload.language:
            raise _bad_request("language_required", "Select a language", "language")
        language = payload.language.lower()
        if language not in supported_languages():
            raise _bad_request(
                "unsupported_language",
                f"Supported languages: {', '.join(supported_languages())}",
                "language",
            )
        if question.allowed_languages and language not in question.allowed_languages:
            raise _bad_request(
                "language_not_allowed",
                f"This question accepts: {', '.join(question.allowed_languages)}",
                "language",
            )
        if not payload.source_code.strip():
            raise _bad_request("empty_submission", "Write some code first", "source_code")

    if question.type is QuestionType.MCQ and not payload.answer.get("selected"):
        raise _bad_request("no_answer_selected", "Select an answer", "answer")

    # A dry run with no sample cases would execute nothing and report a
    # meaningless pass, so refuse it explicitly.
    if (
        payload.is_dry_run
        and question.type is QuestionType.CODE
        and not any(tc.is_sample for tc in question.test_cases)
    ):
        raise _bad_request("no_sample_cases", "This question has no sample tests to run")

    submission = Submission(
        user_id=user.id,
        question_id=question.id,
        session_id=payload.session_id,
        room_id=payload.room_id,
        idempotency_key=idempotency_key,
        type=question.type,
        language=(payload.language or "").lower() or None,
        source_code=payload.source_code,
        answer=payload.answer,
        is_dry_run=payload.is_dry_run,
        status=SubmissionStatus.QUEUED,
        enqueued_at=datetime.now(UTC),
    )
    db.add(submission)

    try:
        await db.flush()
    except IntegrityError:
        # Lost the idempotency race: a concurrent identical request won.
        # Return its submission rather than creating a second one.
        await db.rollback()
        if idempotency_key:
            winner = (
                await db.execute(
                    select(Submission).where(
                        Submission.user_id == user.id,
                        Submission.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if winner is not None:
                return _accepted(winner, deduplicated=True)
        raise

    submission_id = submission.id

    # No enqueue call. The row above, written in QUEUED, *is* the queue entry:
    # workers poll for that status. This is what a database-backed queue buys
    # over a broker -- the write and the enqueue are one transaction, so the
    # "committed but never enqueued" gap that needed an after-commit hook
    # cannot exist.

    log.info(
        "submission.accepted",
        submission_id=str(submission_id),
        question_id=str(question.id),
        type=question.type.value,
        dry_run=payload.is_dry_run,
    )
    return _accepted(submission)


def _accepted(submission: Submission, *, deduplicated: bool = False) -> SubmissionAccepted:
    sid = submission.id
    return SubmissionAccepted(
        id=sid,
        status=submission.status,
        poll_url=f"{settings.api_v1_prefix}/submissions/{sid}",
        websocket_url=f"/ws/submissions/{sid}",
        deduplicated=deduplicated,
    )


async def get_submission(
    db: AsyncSession, user: User, submission_id: uuid.UUID
) -> SubmissionDetail:
    submission = (
        await db.execute(
            select(Submission)
            .where(Submission.id == submission_id)
            .options(selectinload(Submission.results))
        )
    ).scalar_one_or_none()

    if submission is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "submission_not_found", "message": "No such submission"},
        )
    if submission.user_id != user.id and user.role is not UserRole.ADMIN:
        # 404, not 403. Confirming a submission exists but is not yours leaks
        # that the id is real.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "submission_not_found", "message": "No such submission"},
        )

    detail = SubmissionDetail.model_validate(submission)
    detail.results = [
        ResultOut.model_validate(r) for r in sorted(submission.results, key=lambda r: r.ordinal)
    ]

    # Attached if a worker has already produced one. Never generated here:
    # a GET must not block on an LLM call or spend the user's budget.
    if submission.ai_review_id is not None:
        row = await db.get(AIInteraction, submission.ai_review_id)
        if row is not None and row.ok:
            detail.ai_review = dict(row.response)

    return detail


async def list_submissions(
    db: AsyncSession,
    user: User,
    *,
    question_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    include_dry_runs: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> Page[SubmissionSummary]:
    stmt = select(Submission).where(Submission.user_id == user.id)

    if question_id:
        stmt = stmt.where(Submission.question_id == question_id)
    if session_id:
        stmt = stmt.where(Submission.session_id == session_id)
    if not include_dry_runs:
        stmt = stmt.where(Submission.is_dry_run.is_(False))

    rows = (
        (
            await db.execute(
                stmt.order_by(Submission.created_at.desc()).limit(limit + 1).offset(offset)
            )
        )
        .scalars()
        .all()
    )

    return Page[SubmissionSummary](
        items=[SubmissionSummary.model_validate(s) for s in rows[:limit]],
        limit=limit,
        offset=offset,
        has_more=len(rows) > limit,
    )


async def request_hint(db: AsyncSession, user: User, payload) -> dict[str, object]:
    """Generate a hint for a question the user is working on.

    The AI layer is synchronous (the queue worker uses it too), so the
    call is pushed to a thread rather than blocking the event loop for the
    seconds an LLM takes.
    """
    import anyio

    from crucible.db.session import get_sync_session_factory
    from crucible.services.ai_review import generate_hint

    question = await db.get(Question, payload.question_id)
    if question is None or not question.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "question_not_found", "message": "No such question"},
        )

    def _run() -> dict[str, object] | None:
        # A separate sync session: the AI client writes to the audit ledger,
        # and that write belongs to its own short transaction rather than the
        # request's.
        with get_sync_session_factory()() as sync_db:
            sync_question = sync_db.get(Question, payload.question_id)
            if sync_question is None:
                return None
            result = generate_hint(
                sync_db,
                question=sync_question,
                attempt=payload.attempt,
                language=payload.language,
                user_id=user.id,
            )
            sync_db.commit()
            return result

    hint = await anyio.to_thread.run_sync(_run)
    if hint is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "ai_unavailable",
                "message": "No hint available right now",
            },
        )
    return hint
