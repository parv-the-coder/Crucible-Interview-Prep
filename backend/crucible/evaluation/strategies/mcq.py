"""Grade a multiple-choice answer.

Cheap and synchronous, but still routed through the queue like every other
type. Two reasons: the submission status machine stays uniform (one code path
to reason about), and a burst of MCQ submissions cannot starve the API's
request budget.
"""

from __future__ import annotations

from crucible.db.enums import TestCaseOutcome
from crucible.evaluation.strategies.base import (
    CaseResult,
    EvaluationContext,
    EvaluationResult,
    EvaluationStrategy,
)


class McqStrategy(EvaluationStrategy):
    question_type = "mcq"

    def evaluate(self, ctx: EvaluationContext) -> EvaluationResult:
        correct = {str(c).strip().lower() for c in ctx.payload.get("correct", [])}
        if not correct:
            return EvaluationResult(
                score=0.0,
                max_score=100.0,
                passed=False,
                error_message="Question has no answer key",
            )

        raw = ctx.answer.get("selected", [])
        if isinstance(raw, str):
            raw = [raw]
        selected = {str(c).strip().lower() for c in raw}

        allow_partial = bool(ctx.payload.get("allow_partial_credit", len(correct) > 1))

        if allow_partial:
            # Negative marking for wrong picks, floored at zero. Without the
            # penalty, selecting every option scores 100% on a multi-answer
            # question, which makes the score meaningless.
            hits = len(selected & correct)
            wrong = len(selected - correct)
            ratio = max(0.0, (hits - wrong) / len(correct))
            score = round(100.0 * ratio, 2)
        else:
            score = 100.0 if selected == correct else 0.0

        passed = selected == correct
        return EvaluationResult(
            score=score,
            max_score=100.0,
            passed=passed,
            cases=[
                CaseResult(
                    ordinal=0,
                    outcome=TestCaseOutcome.PASSED if passed else TestCaseOutcome.WRONG_ANSWER,
                    stdout=", ".join(sorted(selected)),
                    # The explanation is the teaching moment; it is shown
                    # whether or not the candidate got it right.
                    stderr=str(ctx.payload.get("explanation", "")),
                    is_visible=True,
                )
            ],
        )
