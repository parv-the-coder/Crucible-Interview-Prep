from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from crucible.api.deps import CurrentUser, DbSession, RequireAdmin
from crucible.db.enums import Difficulty, QuestionType
from crucible.schemas.common import Page
from crucible.schemas.question import (
    QuestionCreate,
    QuestionDetail,
    QuestionSummary,
    QuestionUpdate,
)
from crucible.services import questions as service

router = APIRouter(prefix="/questions", tags=["questions"])


@router.get("", response_model=Page[QuestionSummary], summary="Browse questions")
async def list_questions(
    user: CurrentUser,
    db: DbSession,
    topic: str | None = None,
    type: QuestionType | None = None,
    difficulty: Difficulty | None = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[QuestionSummary]:
    return await service.list_questions(
        db,
        user,
        topic=topic,
        question_type=type,
        difficulty=difficulty,
        search=search,
        limit=limit,
        offset=offset,
    )


@router.get("/topics", summary="Topics with question counts")
async def list_topics(user: CurrentUser, db: DbSession) -> list[dict[str, object]]:
    return await service.list_topics(db)


@router.get(
    "/{question_id}",
    response_model=QuestionDetail,
    summary="Question detail",
    description=(
        "Returns sample test cases only. Hidden test cases, MCQ answer keys "
        "and SQL expected rows are never included in any response."
    ),
)
async def get_question(question_id: uuid.UUID, user: CurrentUser, db: DbSession) -> QuestionDetail:
    return await service.get_question(db, user, question_id)


@router.post(
    "",
    response_model=QuestionDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a question (admin)",
)
async def create_question(
    payload: QuestionCreate, admin: RequireAdmin, db: DbSession
) -> QuestionDetail:
    return await service.create_question(db, admin, payload)


@router.patch("/{question_id}", response_model=QuestionDetail, summary="Update a question (admin)")
async def update_question(
    question_id: uuid.UUID, payload: QuestionUpdate, admin: RequireAdmin, db: DbSession
) -> QuestionDetail:
    return await service.update_question(db, question_id, payload)


@router.delete(
    "/{question_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Archive a question (admin)",
    description=(
        "Soft delete. Questions are never hard-deleted: submissions reference "
        "them with ON DELETE RESTRICT, because removing a question would "
        "destroy the history of everyone who answered it."
    ),
)
async def archive_question(question_id: uuid.UUID, admin: RequireAdmin, db: DbSession) -> Response:
    await service.archive_question(db, question_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
