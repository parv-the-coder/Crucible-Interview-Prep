from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from crucible.api.deps import CurrentUser, DbSession
from crucible.schemas.common import Page
from crucible.schemas.room import RoomCreate, RoomDetail, RoomFeedback, RoomJoin, RoomSummary
from crucible.services import rooms as service

router = APIRouter(prefix="/rooms", tags=["interview rooms"])


@router.post(
    "",
    response_model=RoomDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create an interview room",
    description=(
        "Returns a short join code to share, and a WebSocket URL. The creator "
        "becomes the interviewer."
    ),
)
async def create_room(payload: RoomCreate, user: CurrentUser, db: DbSession) -> RoomDetail:
    return await service.create_room(db, user, payload)


@router.post("/join", response_model=RoomDetail, summary="Join by code")
async def join_room(payload: RoomJoin, user: CurrentUser, db: DbSession) -> RoomDetail:
    return await service.join_room(db, user, payload.join_code)


@router.get("", response_model=Page[RoomSummary], summary="My rooms")
async def list_rooms(
    user: CurrentUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[RoomSummary]:
    return await service.list_rooms(db, user, limit=limit, offset=offset)


@router.get("/{room_id}", response_model=RoomDetail, summary="Room detail")
async def get_room(room_id: uuid.UUID, user: CurrentUser, db: DbSession) -> RoomDetail:
    return await service.get_room(db, user, room_id)


@router.get(
    "/{room_id}/replay",
    summary="Replay a recorded session",
    description=(
        "Every event in order: edits, chat, language changes and runs. This is "
        "what the append-only log buys beyond conflict resolution."
    ),
)
async def replay(room_id: uuid.UUID, user: CurrentUser, db: DbSession) -> list[dict]:
    return await service.replay(db, user, room_id)


@router.post("/{room_id}/end", response_model=RoomDetail, summary="End the interview")
async def end_room(room_id: uuid.UUID, user: CurrentUser, db: DbSession) -> RoomDetail:
    return await service.end_room(db, user, room_id)


@router.put(
    "/{room_id}/feedback",
    response_model=RoomDetail,
    summary="Save interviewer notes",
    description="Interviewer only. Notes are private to them, not shown to the candidate.",
)
async def save_feedback(
    room_id: uuid.UUID, payload: RoomFeedback, user: CurrentUser, db: DbSession
) -> RoomDetail:
    return await service.save_feedback(
        db,
        user,
        room_id,
        payload.notes,
        {"rating": payload.rating, "recommendation": payload.recommendation},
    )
