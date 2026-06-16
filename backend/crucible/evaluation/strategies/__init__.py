"""Strategy registry.

The factory is a dict lookup rather than an if/elif chain so that adding a
question type never edits existing dispatch code.
"""

from __future__ import annotations

from crucible.evaluation.sandbox import SandboxBackend, get_sandbox
from crucible.evaluation.strategies.base import (
    CaseResult,
    EvaluationContext,
    EvaluationResult,
    EvaluationStrategy,
    TestCaseSpec,
)
from crucible.evaluation.strategies.code import CodeStrategy
from crucible.evaluation.strategies.mcq import McqStrategy
from crucible.evaluation.strategies.sql import SqlStrategy


class UnsupportedQuestionTypeError(ValueError):
    pass


def build_strategy(
    question_type: str, *, sandbox: SandboxBackend | None = None
) -> EvaluationStrategy:
    """Return the strategy for a question type.

    Only CodeStrategy needs a sandbox, so it is resolved lazily -- grading an
    MCQ must not require a working container runtime.
    """
    key = question_type.strip().lower()
    if key == "code":
        return CodeStrategy(sandbox or get_sandbox())
    if key == "mcq":
        return McqStrategy()
    if key == "sql":
        return SqlStrategy()
    raise UnsupportedQuestionTypeError(
        f"No evaluation strategy for question type {question_type!r}"
    )


def supported_question_types() -> list[str]:
    return ["code", "mcq", "sql"]


__all__ = [
    "CaseResult",
    "CodeStrategy",
    "EvaluationContext",
    "EvaluationResult",
    "EvaluationStrategy",
    "McqStrategy",
    "SqlStrategy",
    "TestCaseSpec",
    "UnsupportedQuestionTypeError",
    "build_strategy",
    "supported_question_types",
]
