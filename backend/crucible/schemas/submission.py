from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from crucible.db.enums import QuestionType, SubmissionStatus, TestCaseOutcome
from crucible.schemas.common import ORMModel


class SubmissionCreate(BaseModel):
    question_id: uuid.UUID
    language: str | None = None
    source_code: str = Field(default="", max_length=200_000)
    answer: dict[str, Any] = Field(default_factory=dict)
    session_id: uuid.UUID | None = None
    room_id: uuid.UUID | None = None
    # "Run" vs "Submit". A dry run executes sample cases only and never
    # affects ratings, mastery or session scoring.
    is_dry_run: bool = False

    @model_validator(mode="after")
    def _needs_content(self) -> SubmissionCreate:
        if not self.source_code.strip() and not self.answer:
            raise ValueError("Submission must contain either source_code or answer")
        return self


class ResultOut(ORMModel):
    ordinal: int
    outcome: TestCaseOutcome
    execution_ms: int
    memory_kb: int
    # Only ever populated for sample cases -- see SubmissionDetail.
    stdout: str = ""
    stderr: str = ""
    is_visible: bool


class SubmissionSummary(ORMModel):
    id: uuid.UUID
    question_id: uuid.UUID
    type: QuestionType
    language: str | None
    status: SubmissionStatus
    score: float
    max_score: float
    passed: bool
    cases_passed: int
    cases_total: int
    is_dry_run: bool
    execution_ms: int
    created_at: datetime


class SubmissionDetail(SubmissionSummary):
    source_code: str
    compile_output: str
    error_message: str
    queue_wait_ms: int
    peak_memory_kb: int
    started_at: datetime | None
    finished_at: datetime | None
    results: list[ResultOut] = Field(default_factory=list)
    ai_review: dict[str, Any] | None = None


class SubmissionAccepted(BaseModel):
    """202 response.

    The submission id is returned immediately; the client then either polls
    GET /submissions/{id} or -- preferably -- subscribes over WebSocket.
    """

    id: uuid.UUID
    status: SubmissionStatus
    poll_url: str
    websocket_url: str
    # True when an idempotency key matched an existing submission and no new
    # evaluation was queued.
    deduplicated: bool = False


class HintRequest(BaseModel):
    question_id: uuid.UUID
    language: str = "python"
    # What they have written so far. A hint for an empty editor is different
    # from one for a nearly-working attempt.
    attempt: str = Field(default="", max_length=50_000)
