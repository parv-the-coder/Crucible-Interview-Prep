from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from crucible.db.enums import Difficulty, QuestionType
from crucible.schemas.common import ORMModel


class TestCaseIn(BaseModel):
    stdin: str = ""
    expected_stdout: str
    is_sample: bool = False
    weight: float = Field(default=1.0, gt=0)
    explanation: str = ""


class TestCaseOut(ORMModel):
    """Sample cases only.

    There is no variant of this that includes hidden cases. Making it
    impossible to serialise them is a stronger guarantee than remembering to
    filter at each call site.
    """

    id: uuid.UUID
    ordinal: int
    stdin: str
    expected_stdout: str
    explanation: str


class QuestionCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=140)
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1)
    constraints_md: str = ""
    type: QuestionType
    difficulty: Difficulty
    topic: str = Field(min_length=1, max_length=80)
    tags: list[str] = Field(default_factory=list)
    time_limit_ms: int = Field(default=5000, ge=100, le=60000)
    memory_limit_mb: int = Field(default=256, ge=16, le=2048)
    allowed_languages: list[str] = Field(default_factory=list)
    starter_code: dict[str, str] = Field(default_factory=dict)
    reference_solution: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    test_cases: list[TestCaseIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_per_type(self) -> QuestionCreate:
        """Each question type has a different notion of "complete".

        Catching this at the schema boundary means the worker never meets a
        question it cannot grade -- which would otherwise surface as a failed
        submission the candidate gets blamed for.
        """
        if self.type is QuestionType.CODE:
            if not self.test_cases:
                raise ValueError("Code questions require at least one test case")
            if not any(tc.is_sample for tc in self.test_cases):
                raise ValueError("Code questions need at least one sample test case")
            if not self.allowed_languages:
                raise ValueError("Code questions must allow at least one language")
        elif self.type is QuestionType.MCQ:
            choices = self.payload.get("choices")
            correct = self.payload.get("correct")
            if not choices or not isinstance(choices, list):
                raise ValueError("MCQ questions require payload.choices")
            if not correct or not isinstance(correct, list):
                raise ValueError("MCQ questions require payload.correct")
            keys = {str(c.get("key", "")).lower() for c in choices if isinstance(c, dict)}
            unknown = {str(c).lower() for c in correct} - keys
            if unknown:
                raise ValueError(f"payload.correct references unknown choices: {sorted(unknown)}")
        elif self.type is QuestionType.SQL:
            if not self.payload.get("schema_sql"):
                raise ValueError("SQL questions require payload.schema_sql")
            if self.payload.get("expected_rows") is None:
                raise ValueError("SQL questions require payload.expected_rows")
        return self


class QuestionUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    prompt: str | None = None
    constraints_md: str | None = None
    difficulty: Difficulty | None = None
    topic: str | None = Field(default=None, max_length=80)
    tags: list[str] | None = None
    time_limit_ms: int | None = Field(default=None, ge=100, le=60000)
    memory_limit_mb: int | None = Field(default=None, ge=16, le=2048)
    allowed_languages: list[str] | None = None
    starter_code: dict[str, str] | None = None
    payload: dict[str, Any] | None = None
    is_active: bool | None = None


class QuestionSummary(ORMModel):
    """List view. Deliberately excludes prompt and payload.

    A 50-item listing that carried every prompt body would be megabytes; the
    browser only renders titles and badges.
    """

    id: uuid.UUID
    slug: str
    title: str
    type: QuestionType
    difficulty: Difficulty
    topic: str
    tags: list[str]
    rating: float
    attempt_count: int
    pass_count: int


class QuestionDetail(ORMModel):
    id: uuid.UUID
    slug: str
    title: str
    prompt: str
    constraints_md: str
    type: QuestionType
    difficulty: Difficulty
    topic: str
    tags: list[str]
    rating: float
    time_limit_ms: int
    memory_limit_mb: int
    allowed_languages: list[str]
    starter_code: dict[str, Any]
    attempt_count: int
    pass_count: int
    created_at: datetime
    # Populated by the service after stripping the answer key.
    sample_test_cases: list[TestCaseOut] = Field(default_factory=list)
    public_payload: dict[str, Any] = Field(default_factory=dict)
