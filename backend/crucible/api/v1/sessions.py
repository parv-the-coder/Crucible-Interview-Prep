from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status

from crucible.api.deps import CurrentUser, DbSession
from crucible.schemas.common import Page
from crucible.schemas.session import (
    DraftSave,
    SessionCreate,
    SessionDetail,
    SessionResult,
    SessionSummary,
    ViolationOut,
    ViolationReport,
)
from crucible.services import sessions as service

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post(
    "",
    response_model=SessionDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Start a timed test",
    description=(
        "Locks in the question set and starts the clock. The returned "
        "`ends_at` is authoritative; `seconds_remaining` is a convenience for "
        "the client countdown. Only one session may be active at a time."
    ),
)
async def start_session(payload: SessionCreate, user: CurrentUser, db: DbSession) -> SessionDetail:
    return await service.start_session(db, user, payload)


@router.get("", response_model=Page[SessionSummary], summary="My test history")
async def list_sessions(
    user: CurrentUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[SessionSummary]:
    return await service.list_sessions(db, user, limit=limit, offset=offset)


@router.get("/{session_id}", response_model=SessionDetail, summary="Session detail")
async def get_session(session_id: uuid.UUID, user: CurrentUser, db: DbSession) -> SessionDetail:
    return await service.get_session(db, user, session_id)


@router.put(
    "/{session_id}/items/{item_id}/draft",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Autosave an answer",
    description=(
        "Saves work in progress without grading it. Called on a debounce as "
        "the candidate types, so a crashed browser loses nothing."
    ),
)
async def save_draft(
    session_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: DraftSave,
    user: CurrentUser,
    db: DbSession,
) -> Response:
    await service.save_draft(db, user, session_id, item_id, payload)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{session_id}/violations",
    response_model=ViolationOut,
    summary="Report a proctoring event",
    description=(
        "Client-reported tab blur, fullscreen exit or large paste. Two "
        "warnings; the third auto-submits the session. These can be suppressed "
        "by a determined user, so they raise the cost of casual cheating "
        "rather than preventing it."
    ),
)
async def report_violation(
    session_id: uuid.UUID,
    payload: ViolationReport,
    request: Request,
    user: CurrentUser,
    db: DbSession,
) -> ViolationOut:
    return await service.record_violation(
        db,
        user,
        session_id,
        payload.kind,
        payload.detail,
        request_id=getattr(request.state, "request_id", None),
    )


@router.post(
    "/{session_id}/submit",
    response_model=SessionResult,
    summary="Finish the test",
    description=(
        "Queues every drafted answer for grading and closes the session. "
        "Scores fill in as workers finish, so poll this endpoint or the "
        "individual submissions for final results."
    ),
)
async def submit_session(session_id: uuid.UUID, user: CurrentUser, db: DbSession) -> SessionResult:
    return await service.submit_session(db, user, session_id)


@router.get(
    "/{session_id}/result",
    response_model=SessionResult,
    summary="Session result",
    description="Live view. Fills in as queued submissions are graded.",
)
async def get_result(session_id: uuid.UUID, user: CurrentUser, db: DbSession) -> SessionResult:
    session = await service._load(db, user, session_id)
    return await service.build_result(db, session)
