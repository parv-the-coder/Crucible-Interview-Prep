from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from crucible.db.enums import (
    Difficulty,
    QuestionType,
    SessionStatus,
    ViolationAction,
    ViolationKind,
)
from crucible.schemas.common import ORMModel
from crucible.schemas.question import QuestionDetail


class SessionCreate(BaseModel):
    topics: list[str] = Field(default_factory=list, max_length=10)
    question_types: list[QuestionType] = Field(default_factory=list)
    difficulty: Difficulty | None = None
    question_count: int = Field(default=5, ge=1, le=25)
    duration_minutes: int = Field(default=30, ge=5, le=180)
    # Adaptive picks questions near the user's rating instead of using the
    # difficulty filter.
    adaptive: bool = False

    @model_validator(mode="after")
    def _sane_time_budget(self) -> SessionCreate:
        """Refuse a test nobody could finish.

        Three minutes per question is already tight for a coding problem.
        Letting someone start a 25-question test with a 5-minute limit
        produces a guaranteed failure and a support ticket.
        """
        if self.duration_minutes * 60 < self.question_count * 120:
            raise ValueError(
                f"{self.duration_minutes} minutes is too short for "
                f"{self.question_count} questions (allow at least 2 minutes each)"
            )
        return self


class DraftSave(BaseModel):
    """Autosave payload. Never creates a submission."""

    language: str | None = None
    code: str = Field(default="", max_length=200_000)
    answer: dict[str, Any] = Field(default_factory=dict)


class ViolationReport(BaseModel):
    kind: ViolationKind
    detail: dict[str, Any] = Field(default_factory=dict)


class ViolationOut(ORMModel):
    kind: ViolationKind
    action: ViolationAction
    running_count: int
    created_at: datetime


class SessionItemOut(ORMModel):
    id: uuid.UUID
    ordinal: int
    question_id: uuid.UUID
    draft_language: str | None
    draft_code: str
    draft_answer: dict[str, Any]
    score: float
    max_score: float
    final_submission_id: uuid.UUID | None
    question: QuestionDetail | None = None


class SessionSummary(ORMModel):
    id: uuid.UUID
    status: SessionStatus
    topics: list[str]
    duration_seconds: int
    starts_at: datetime
    ends_at: datetime
    submitted_at: datetime | None
    violation_count: int
    total_score: float
    max_score: float
    created_at: datetime


class SessionDetail(SessionSummary):
    items: list[SessionItemOut] = Field(default_factory=list)
    # Server-computed. The browser countdown is a display of this, never the
    # authority on whether the test is still open.
    seconds_remaining: int = 0
    debrief: dict[str, Any] = Field(default_factory=dict)


class SessionResult(BaseModel):
    session_id: uuid.UUID
    status: SessionStatus
    total_score: float
    max_score: float
    percentage: float
    questions_attempted: int
    questions_total: int
    violation_count: int
    per_topic: dict[str, dict[str, float]] = Field(default_factory=dict)
    weakest_topics: list[str] = Field(default_factory=list)
