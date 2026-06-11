"""Question catalogue.

The rule that governs this whole module: **the answer key never leaves the
server**. Hidden test cases, MCQ correct answers and SQL expected rows all live
on the Question row, and every read path here strips them. That is enforced by
`to_detail()` being the only way a question reaches a response model.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from crucible.db.enums import Difficulty, QuestionType, UserRole
from crucible.db.models import Question, TestCase, User
from crucible.schemas.common import Page
from crucible.schemas.question import (
    QuestionCreate,
    QuestionDetail,
    QuestionSummary,
    QuestionUpdate,
    TestCaseOut,
)


def _public_payload(question: Question) -> dict[str, Any]:
    """Strip the answer key from a question's type-specific payload.

    Allow-list, not deny-list. A deny-list means a new payload field is exposed
    by default, and the first time that happens it is an answer leak.
    """
    payload = question.payload or {}

    if question.type is QuestionType.MCQ:
        return {
            "choices": payload.get("choices", []),
            "multiple": len(payload.get("correct", [])) > 1,
        }
    if question.type is QuestionType.SQL:
        return {
            "schema_sql": payload.get("schema_sql", ""),
            "sample_rows": payload.get("sample_rows", []),
            "dialect": "sqlite",
        }
    if question.type in (QuestionType.SYSTEM_DESIGN, QuestionType.BEHAVIORAL):
        # Criteria names help the candidate; weights would let them game it.
        rubric = payload.get("rubric", [])
        return {"rubric_criteria": [c.get("criterion") for c in rubric if isinstance(c, dict)]}
    return {}


def to_detail(question: Question) -> QuestionDetail:
    """The only sanctioned way to serialise a question for a candidate."""
    detail = QuestionDetail.model_validate(question)
    detail.sample_test_cases = [
        TestCaseOut.model_validate(tc) for tc in question.test_cases if tc.is_sample
    ]
    detail.public_payload = _public_payload(question)
    return detail


def _visible(stmt: Select, user: User) -> Select:
    """Non-admins only ever see active questions."""
    if user.role is UserRole.ADMIN:
        return stmt
    return stmt.where(Question.is_active.is_(True))


async def list_questions(
    db: AsyncSession,
    user: User,
    *,
    topic: str | None = None,
    question_type: QuestionType | None = None,
    difficulty: Difficulty | None = None,
    search: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> Page[QuestionSummary]:
    stmt = _visible(select(Question), user)

    if topic:
        stmt = stmt.where(Question.topic == topic)
    if question_type:
        stmt = stmt.where(Question.type == question_type)
    if difficulty:
        stmt = stmt.where(Question.difficulty == difficulty)
    if search:
        term = f"%{search.strip()}%"
        stmt = stmt.where(or_(Question.title.ilike(term), Question.topic.ilike(term)))

    # Fetch one extra row instead of running COUNT(*). Knowing whether a next
    # page exists is what the UI needs, and it is far cheaper than counting a
    # filtered set on every keystroke.
    rows = (
        (
            await db.execute(
                stmt.order_by(Question.difficulty, Question.title).limit(limit + 1).offset(offset)
            )
        )
        .scalars()
        .all()
    )

    has_more = len(rows) > limit
    return Page[QuestionSummary](
        items=[QuestionSummary.model_validate(q) for q in rows[:limit]],
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


async def get_question(db: AsyncSession, user: User, question_id: uuid.UUID) -> QuestionDetail:
    stmt = _visible(
        select(Question)
        .where(Question.id == question_id)
        .options(selectinload(Question.test_cases)),
        user,
    )
    question = (await db.execute(stmt)).scalar_one_or_none()
    if question is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "question_not_found", "message": "No such question"},
        )
    return to_detail(question)


async def list_topics(db: AsyncSession) -> list[dict[str, Any]]:
    """Topics with question counts, for the browser's filter sidebar."""
    rows = (
        await db.execute(
            select(Question.topic, func.count(Question.id))
            .where(Question.is_active.is_(True))
            .group_by(Question.topic)
            .order_by(Question.topic)
        )
    ).all()
    return [{"topic": topic, "count": count} for topic, count in rows]


async def create_question(
    db: AsyncSession, author: User, payload: QuestionCreate
) -> QuestionDetail:
    question = Question(
        slug=payload.slug,
        title=payload.title,
        prompt=payload.prompt,
        constraints_md=payload.constraints_md,
        type=payload.type,
        difficulty=payload.difficulty,
        topic=payload.topic,
        tags=payload.tags,
        time_limit_ms=payload.time_limit_ms,
        memory_limit_mb=payload.memory_limit_mb,
        allowed_languages=payload.allowed_languages,
        starter_code=payload.starter_code,
        reference_solution=payload.reference_solution,
        payload=payload.payload,
        created_by_id=author.id,
    )
    for ordinal, case in enumerate(payload.test_cases):
        question.test_cases.append(
            TestCase(
                ordinal=ordinal,
                stdin=case.stdin,
                expected_stdout=case.expected_stdout,
                is_sample=case.is_sample,
                weight=case.weight,
                explanation=case.explanation,
            )
        )

    db.add(question)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "slug_taken",
                "message": f"A question with slug {payload.slug!r} already exists",
                "field": "slug",
            },
        ) from exc

    await db.refresh(question, ["test_cases"])
    return to_detail(question)


async def update_question(
    db: AsyncSession, question_id: uuid.UUID, payload: QuestionUpdate
) -> QuestionDetail:
    question = (
        await db.execute(
            select(Question)
            .where(Question.id == question_id)
            .options(selectinload(Question.test_cases))
        )
    ).scalar_one_or_none()
    if question is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "question_not_found", "message": "No such question"},
        )

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(question, field, value)

    await db.flush()
    return to_detail(question)


async def archive_question(db: AsyncSession, question_id: uuid.UUID) -> None:
    """Soft delete.

    Hard deletion is refused by the schema anyway: submissions reference
    questions with ON DELETE RESTRICT, because deleting a question would
    silently destroy the history of everyone who ever answered it.
    """
    question = await db.get(Question, question_id)
    if question is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "question_not_found", "message": "No such question"},
        )
    question.is_active = False
    await db.flush()
