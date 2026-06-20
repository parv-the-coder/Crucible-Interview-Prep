"""Elo rating and adaptive question selection.

Treats "user attempts question" as a match between two rated players. Both
ratings move by the surprise in the result, so question difficulty
self-corrects from evidence instead of depending on the author's label.

See docs/adr/0010-elo-for-adaptive-difficulty.md for why Elo and not IRT.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from crucible.core.logging import get_logger
from crucible.db.models import Question, TopicMastery, User

log = get_logger(__name__)

DEFAULT_RATING = 1200.0

# K controls how far a rating moves per result.
# Users start volatile so a new account converges quickly, then settle.
K_USER_NEW = 32.0
K_USER_ESTABLISHED = 16.0
ESTABLISHED_AFTER_ATTEMPTS = 30
# Questions move far more slowly: a question accumulates evidence from many
# users, so any single result should barely shift it.
K_QUESTION = 8.0


def expected_score(player_rating: float, opponent_rating: float) -> float:
    """Standard Elo expectation: probability the player "wins".

    The 400 constant is the Elo convention -- a 400-point gap implies roughly
    a 10:1 expected result.
    """
    return 1.0 / (1.0 + 10.0 ** ((opponent_rating - player_rating) / 400.0))


def k_factor(attempts: int) -> float:
    return K_USER_NEW if attempts < ESTABLISHED_AFTER_ATTEMPTS else K_USER_ESTABLISHED


def apply_submission_outcome(
    db: Session,
    user_id: uuid.UUID,
    question_id: uuid.UUID,
    *,
    passed: bool,
    score: float,
) -> dict[str, float]:
    """Update user, question and topic ratings after a graded submission."""
    user = db.get(User, user_id)
    question = db.get(Question, question_id)
    if user is None or question is None:
        return {}

    # Fractional score, not binary pass/fail. Passing 8 of 10 test cases is
    # real evidence about ability, and binarising it discards most of the
    # signal every submission carries.
    actual = max(0.0, min(1.0, score / 100.0))
    expected = expected_score(user.rating, question.rating)
    surprise = actual - expected

    mastery = db.execute(
        select(TopicMastery).where(
            TopicMastery.user_id == user_id, TopicMastery.topic == question.topic
        )
    ).scalar_one_or_none()
    attempts = mastery.attempts if mastery else 0

    k_user = k_factor(attempts)
    user.rating = round(max(0.0, user.rating + k_user * surprise), 2)
    question.rating = round(max(0.0, question.rating - K_QUESTION * surprise), 2)
    question.attempt_count += 1
    if passed:
        question.pass_count += 1

    # Upsert rather than select-then-insert: two submissions finishing at the
    # same moment would otherwise race on the unique (user_id, topic) index.
    now = datetime.now(UTC)
    topic_expected = expected_score(mastery.rating if mastery else DEFAULT_RATING, question.rating)
    topic_delta = k_user * (actual - topic_expected)

    stmt = insert(TopicMastery).values(
        user_id=user_id,
        topic=question.topic,
        rating=round(DEFAULT_RATING + topic_delta, 2),
        attempts=1,
        passes=1 if passed else 0,
        last_difficulty=question.difficulty,
        last_attempted_at=now,
    )
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=[TopicMastery.user_id, TopicMastery.topic],
            set_={
                "rating": TopicMastery.rating + topic_delta,
                "attempts": TopicMastery.attempts + 1,
                "passes": TopicMastery.passes + (1 if passed else 0),
                "last_difficulty": question.difficulty,
                "last_attempted_at": now,
            },
        )
    )

    log.info(
        "rating.updated",
        user_id=str(user_id),
        question_id=str(question_id),
        score=score,
        expected=round(expected, 3),
        user_rating=user.rating,
        question_rating=question.rating,
    )
    return {
        "user_rating": user.rating,
        "question_rating": question.rating,
        "expected": round(expected, 3),
        "actual": actual,
    }


async def pick_questions(
    db,
    user: User,
    *,
    topics: list[str] | None = None,
    count: int = 5,
    question_types: list[str] | None = None,
) -> list[Question]:
    """Choose questions near the user's rating.

    Targets questions the user has roughly a 50-70% chance of solving, which
    is where learning is fastest: too easy teaches nothing, too hard just
    demoralises. Concretely that means questions rated at or slightly above
    the user's current rating.
    """
    from sqlalchemy import and_, func, not_

    from crucible.db.models import Submission

    # Skip questions already solved -- re-serving a solved problem in a
    # practice test wastes one of the user's few slots.
    solved = select(Submission.question_id).where(
        and_(
            Submission.user_id == user.id,
            Submission.passed.is_(True),
            Submission.is_dry_run.is_(False),
        )
    )

    target = user.rating + 50.0
    stmt = (
        select(Question)
        .where(Question.is_active.is_(True), not_(Question.id.in_(solved)))
        .order_by(func.abs(Question.rating - target))
        .limit(count)
    )
    if topics:
        stmt = stmt.where(Question.topic.in_(topics))
    if question_types:
        stmt = stmt.where(Question.type.in_(question_types))

    rows = (await db.execute(stmt)).scalars().all()

    # Fall back to including solved questions rather than returning an empty
    # test, which would look like a broken feature to the candidate.
    if len(rows) < count:
        backfill = select(Question).where(Question.is_active.is_(True))
        if topics:
            backfill = backfill.where(Question.topic.in_(topics))
        if question_types:
            backfill = backfill.where(Question.type.in_(question_types))
        extra = (
            (await db.execute(backfill.order_by(func.abs(Question.rating - target)).limit(count)))
            .scalars()
            .all()
        )
        seen = {q.id for q in rows}
        rows = list(rows) + [q for q in extra if q.id not in seen]

    return list(rows)[:count]
