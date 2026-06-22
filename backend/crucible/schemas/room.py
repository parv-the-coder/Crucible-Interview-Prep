from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from crucible.db.enums import RoomRole, RoomStatus
from crucible.schemas.common import ORMModel


class RoomCreate(BaseModel):
    title: str = Field(default="", max_length=200)
    question_id: uuid.UUID | None = None
    language: str = "python"
    is_recorded: bool = True


class RoomJoin(BaseModel):
    join_code: str = Field(max_length=12)


class ParticipantOut(ORMModel):
    user_id: uuid.UUID
    role: RoomRole
    joined_at: datetime | None
    left_at: datetime | None


class RoomSummary(ORMModel):
    id: uuid.UUID
    join_code: str
    title: str
    status: RoomStatus
    language: str
    host_id: uuid.UUID
    question_id: uuid.UUID | None
    created_at: datetime
    ended_at: datetime | None


class RoomDetail(RoomSummary):
    document: str
    document_version: int
    notes: str
    participants: list[ParticipantOut] = Field(default_factory=list)
    # Relative path; the client resolves it against its own origin so this
    # works unchanged behind a proxy or a different host.
    websocket_url: str = ""


class RoomFeedback(BaseModel):
    notes: str = Field(default="", max_length=20_000)
    rating: int | None = Field(default=None, ge=1, le=5)
    recommendation: str | None = Field(default=None, max_length=40)
