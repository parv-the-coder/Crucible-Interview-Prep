from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from crucible.api.deps import CurrentUser, DbSession, get_idempotency_key
from crucible.evaluation.sandbox import supported_languages
from crucible.schemas.common import Page
from crucible.schemas.submission import (
    HintRequest,
    SubmissionAccepted,
    SubmissionCreate,
    SubmissionDetail,
    SubmissionSummary,
)
from crucible.services import submissions as service

router = APIRouter(prefix="/submissions", tags=["submissions"])


@router.post(
    "",
    response_model=SubmissionAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a solution",
    description=(
        "Returns immediately with 202. Evaluation happens on a worker; poll "
        "`poll_url` or subscribe to `websocket_url` for the result.\n\n"
        "Send an `Idempotency-Key` header to make retries safe: a repeated "
        "request with the same key returns the original submission instead of "
        "queueing a second evaluation."
    ),
)
async def create_submission(
    payload: SubmissionCreate,
    user: CurrentUser,
    db: DbSession,
    idempotency_key: Annotated[str | None, Depends(get_idempotency_key)] = None,
) -> SubmissionAccepted:
    return await service.create_submission(db, user, payload, idempotency_key=idempotency_key)


@router.get("", response_model=Page[SubmissionSummary], summary="My submissions")
async def list_submissions(
    user: CurrentUser,
    db: DbSession,
    question_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    include_dry_runs: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[SubmissionSummary]:
    return await service.list_submissions(
        db,
        user,
        question_id=question_id,
        session_id=session_id,
        include_dry_runs=include_dry_runs,
        limit=limit,
        offset=offset,
    )


@router.get("/languages", summary="Supported execution languages")
async def languages(user: CurrentUser) -> dict[str, list[str]]:
    return {"languages": supported_languages()}


@router.get("/{submission_id}", response_model=SubmissionDetail, summary="Submission detail")
async def get_submission(
    submission_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> SubmissionDetail:
    return await service.get_submission(db, user, submission_id)


@router.post(
    "/hint",
    summary="Ask for a hint",
    description=(
        "Returns one nudge toward the idea the candidate is missing, never a "
        "solution. Counts against the daily AI budget. Returns 503 when no AI "
        "provider is configured, which callers should treat as 'no hint "
        "available' rather than an error."
    ),
)
async def get_hint(payload: HintRequest, user: CurrentUser, db: DbSession) -> dict[str, object]:
    return await service.request_hint(db, user, payload)
