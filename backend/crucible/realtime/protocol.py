"""Wire protocol for interview rooms.

Every frame is JSON with a `type`. Kept small and explicit rather than generic:
a typed protocol is something the frontend can be written against and the
server can validate, which a free-form event bus is not.
"""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, Field


class ClientMessageType(enum.StrEnum):
    EDIT = "edit"
    CURSOR = "cursor"
    CHAT = "chat"
    LANGUAGE = "language"
    RUN = "run"
    PING = "ping"


class ServerMessageType(enum.StrEnum):
    # Sent once on connect: the full document plus who else is here.
    SNAPSHOT = "snapshot"
    EDIT = "edit"
    # Sent when an edit is rejected; carries the ops the client missed.
    REBASE = "rebase"
    CURSOR = "cursor"
    CHAT = "chat"
    LANGUAGE = "language"
    PRESENCE = "presence"
    RUN_RESULT = "run_result"
    ERROR = "error"
    PONG = "pong"


class EditOp(BaseModel):
    """A replace over a character range.

    Ranges rather than per-character inserts: an editor already gives us
    (from, to, text) on every change, and a coarser op means fewer round trips
    without needing transform functions. See ADR-0009.
    """

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str = Field(default="", max_length=100_000)

    def apply(self, document: str) -> str:
        start = max(0, min(self.start, len(document)))
        end = max(start, min(self.end, len(document)))
        return document[:start] + self.text + document[end:]


class ClientMessage(BaseModel):
    type: ClientMessageType
    # The document version this message was composed against. The server
    # rejects anything stale, which is the whole concurrency control.
    version: int | None = None
    op: EditOp | None = None
    text: str | None = Field(default=None, max_length=4000)
    language: str | None = None
    cursor: int | None = None
    question_id: str | None = None


class Participant(BaseModel):
    user_id: str
    display_name: str
    role: str
    cursor: int = 0


class ServerMessage(BaseModel):
    type: ServerMessageType
    version: int | None = None
    op: EditOp | None = None
    # Ops the client missed, oldest first, so it can catch up without a reload.
    ops: list[dict[str, Any]] = Field(default_factory=list)
    document: str | None = None
    language: str | None = None
    text: str | None = None
    actor: str | None = None
    actor_name: str | None = None
    participants: list[Participant] = Field(default_factory=list)
    cursor: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    message: str | None = None

    def dump(self) -> str:
        return self.model_dump_json(exclude_none=True)
