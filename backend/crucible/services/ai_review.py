"""AI feedback on a graded submission.

Runs after evaluation, on its own queue, and is entirely optional. The score
was decided by the test cases before this ran; nothing here can change it.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from crucible.ai import AIRequest, complete_or_none
from crucible.ai.prompts import (
    CODE_REVIEW_SCHEMA,
    CODE_REVIEW_SYSTEM,
    DEBRIEF_SCHEMA,
    DEBRIEF_SYSTEM,
    FOLLOW_UP_SCHEMA,
    FOLLOW_UP_SYSTEM,
    HINT_SCHEMA,
    HINT_SYSTEM,
    code_review_prompt,
    debrief_prompt,
    follow_up_prompt,
    hint_prompt,
)
from crucible.core.config import settings
from crucible.core.logging import get_logger
from crucible.db.enums import AIPurpose, QuestionType, SubmissionStatus
from crucible.db.models import AIInteraction, Question, Submission

log = get_logger(__name__)

# Rubric scores are advisory. They live in the review payload and are never
# written to Submission.score, which only the test cases set.
MAX_SOURCE_CHARS = 12_000


def review_submission(db: Session, submission_id: uuid.UUID) -> dict[str, Any]:
    """Generate feedback for one completed submission."""
    if not settings.ai_enabled:
        return {"submission_id": str(submission_id), "skipped": "ai_disabled"}

    submission = db.execute(
        select(Submission)
        .where(Submission.id == submission_id)
        .options(selectinload(Submission.results))
    ).scalar_one_or_none()

    if submission is None:
        return {"submission_id": str(submission_id), "skipped": "not_found"}
    if submission.status is not SubmissionStatus.COMPLETED:
        # Reviewing a failed or still-running submission would describe code
        # that was never actually executed.
        return {"submission_id": str(submission_id), "skipped": "not_completed"}
    if submission.is_dry_run:
        return {"submission_id": str(submission_id), "skipped": "dry_run"}
    if submission.type is not QuestionType.CODE:
        # MCQ and SQL already carry a written explanation from the question
        # author, which is better than a generated one.
        return {"submission_id": str(submission_id), "skipped": "not_code"}

    question = db.get(Question, submission.question_id)
    if question is None:
        return {"submission_id": str(submission_id), "skipped": "question_missing"}

    failing = sorted({r.outcome.value for r in submission.results if r.outcome.value != "passed"})

    review = complete_or_none(
        db,
        AIRequest(
            prompt=code_review_prompt(
                title=question.title,
                prompt_md=question.prompt,
                language=submission.language or "python",
                # Truncated so one enormous submission cannot blow the context
                # window or the token budget.
                source=submission.source_code[:MAX_SOURCE_CHARS],
                passed=submission.passed,
                cases_passed=submission.cases_passed,
                cases_total=submission.cases_total,
                failing_outcomes=failing,
            ),
            purpose=AIPurpose.CODE_REVIEW,
            system=CODE_REVIEW_SYSTEM,
            schema=CODE_REVIEW_SCHEMA,
            user_id=submission.user_id,
            submission_id=submission.id,
        ),
    )

    if review is None:
        log.info("ai_review.unavailable", submission_id=str(submission_id))
        return {"submission_id": str(submission_id), "skipped": "provider_unavailable"}

    payload: dict[str, Any] = dict(review.data)

    # A follow-up question only makes sense once they have actually solved it.
    if submission.passed:
        follow_up = complete_or_none(
            db,
            AIRequest(
                prompt=follow_up_prompt(
                    title=question.title,
                    language=submission.language or "python",
                    source=submission.source_code[:MAX_SOURCE_CHARS],
                ),
                purpose=AIPurpose.FOLLOW_UP,
                system=FOLLOW_UP_SYSTEM,
                schema=FOLLOW_UP_SCHEMA,
                user_id=submission.user_id,
                submission_id=submission.id,
            ),
        )
        if follow_up is not None:
            payload["follow_up"] = follow_up.data

    db.flush()

    # Point the submission at the review row so the API can serve it without
    # re-running anything, and store the composed document on that row.
    #
    # The follow-up was a second provider call and has its own ledger row, so
    # the ledger still records one row per call. This row additionally holds
    # the assembled review the API returns, which saves the read path from
    # stitching two rows together on every request.
    latest = db.execute(
        select(AIInteraction)
        .where(
            AIInteraction.submission_id == submission.id,
            AIInteraction.purpose == AIPurpose.CODE_REVIEW,
            AIInteraction.ok.is_(True),
        )
        .order_by(AIInteraction.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is not None:
        latest.response = payload
        submission.ai_review_id = latest.id

    log.info(
        "ai_review.completed",
        submission_id=str(submission_id),
        cached=review.cached,
        passed=submission.passed,
    )
    return {"submission_id": str(submission_id), "ok": True, "cached": review.cached}


def generate_hint(
    db: Session, *, question: Question, attempt: str, language: str, user_id: uuid.UUID
) -> dict[str, Any] | None:
    """A nudge for someone who is stuck. Counts against their daily budget."""
    response = complete_or_none(
        db,
        AIRequest(
            prompt=hint_prompt(
                title=question.title,
                prompt_md=question.prompt,
                attempt=attempt[:MAX_SOURCE_CHARS],
                language=language,
            ),
            purpose=AIPurpose.HINT,
            system=HINT_SYSTEM,
            schema=HINT_SCHEMA,
            user_id=user_id,
        ),
    )
    return response.data if response else None


def generate_debrief(
    db: Session,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    percentage: float,
    per_topic: dict[str, dict[str, float]],
) -> dict[str, Any] | None:
    """Summarise a finished test."""
    if not per_topic:
        return None
    response = complete_or_none(
        db,
        AIRequest(
            prompt=debrief_prompt(percentage=percentage, per_topic=per_topic),
            purpose=AIPurpose.SESSION_DEBRIEF,
            system=DEBRIEF_SYSTEM,
            schema=DEBRIEF_SCHEMA,
            user_id=user_id,
            session_id=session_id,
        ),
    )
    return response.data if response else None
