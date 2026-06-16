"""Evaluation strategy contract.

One strategy per question type. Adding a new type (system design, behavioural)
means adding a class and registering it -- no branching added to the worker.
This is the same Strategy + Factory shape v1 used; what changed is that a
strategy now returns rich per-case results instead of a JSON string, and is
given an explicit, side-effect-free context rather than ORM objects it might
mutate.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from crucible.db.enums import TestCaseOutcome


@dataclass(frozen=True, slots=True)
class TestCaseSpec:
    id: uuid.UUID | None
    ordinal: int
    stdin: str
    expected_stdout: str
    is_sample: bool
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    """Everything a strategy may read.

    Deliberately a plain dataclass rather than the ORM rows: a strategy that
    cannot touch a Session cannot accidentally commit, lazy-load across a
    closed connection, or leave the caller's transaction in a surprising state.
    """

    submission_id: uuid.UUID
    question_id: uuid.UUID
    question_type: str
    language: str | None
    source_code: str
    answer: dict[str, Any]
    payload: dict[str, Any]
    test_cases: tuple[TestCaseSpec, ...]
    time_limit_ms: int
    memory_limit_mb: int
    is_dry_run: bool = False


@dataclass(slots=True)
class CaseResult:
    ordinal: int
    outcome: TestCaseOutcome
    test_case_id: uuid.UUID | None = None
    execution_ms: int = 0
    memory_kb: int = 0
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    is_visible: bool = False

    @property
    def passed(self) -> bool:
        return self.outcome is TestCaseOutcome.PASSED


@dataclass(slots=True)
class EvaluationResult:
    score: float
    max_score: float
    passed: bool
    cases: list[CaseResult] = field(default_factory=list)
    compile_output: str = ""
    error_message: str = ""
    execution_ms: int = 0
    peak_memory_kb: int = 0

    @property
    def cases_passed(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def cases_total(self) -> int:
        return len(self.cases)


class EvaluationStrategy(ABC):
    """Grades one submission. Must be pure with respect to the database."""

    question_type: str

    @abstractmethod
    def evaluate(self, ctx: EvaluationContext) -> EvaluationResult: ...

    @staticmethod
    def normalise(text: str) -> str:
        """Compare outputs the way a human would.

        Trailing whitespace and CRLF are not wrong answers. Being strict about
        them generates support tickets, not signal -- v1 compared raw strings
        and failed Windows users for their line endings.
        """
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        return "\n".join(line.rstrip() for line in lines).strip()
