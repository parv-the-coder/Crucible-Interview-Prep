"""Grade a code submission by running it against test cases."""

from __future__ import annotations

from crucible.db.enums import TestCaseOutcome
from crucible.evaluation.sandbox import ExecOutcome, ExecutionRequest, SandboxBackend
from crucible.evaluation.strategies.base import (
    CaseResult,
    EvaluationContext,
    EvaluationResult,
    EvaluationStrategy,
)

# Sandbox outcome -> per-case outcome.
_OUTCOME_MAP = {
    ExecOutcome.TIMEOUT: TestCaseOutcome.TIMEOUT,
    ExecOutcome.RUNTIME_ERROR: TestCaseOutcome.RUNTIME_ERROR,
    ExecOutcome.COMPILE_ERROR: TestCaseOutcome.COMPILE_ERROR,
}


class CodeStrategy(EvaluationStrategy):
    question_type = "code"

    def __init__(self, sandbox: SandboxBackend) -> None:
        self._sandbox = sandbox

    def evaluate(self, ctx: EvaluationContext) -> EvaluationResult:
        if not ctx.source_code.strip():
            return EvaluationResult(
                score=0.0, max_score=100.0, passed=False, error_message="Empty submission"
            )
        if not ctx.language:
            return EvaluationResult(
                score=0.0, max_score=100.0, passed=False, error_message="No language selected"
            )

        # A dry run ("Run") executes sample cases only and is never graded.
        cases = (
            [c for c in ctx.test_cases if c.is_sample] if ctx.is_dry_run else list(ctx.test_cases)
        )
        if not cases:
            return EvaluationResult(
                score=0.0,
                max_score=100.0,
                passed=False,
                error_message="Question has no test cases",
            )

        timeout_s = max(1, round(ctx.time_limit_ms / 1000))
        results: list[CaseResult] = []
        earned = 0.0
        total_weight = sum(c.weight for c in cases) or 1.0
        total_ms = 0
        peak_kb = 0
        compile_output = ""
        compile_failed = False

        for case in cases:
            run = self._sandbox.execute(
                ExecutionRequest(
                    language=ctx.language,
                    source=ctx.source_code,
                    stdin=case.stdin,
                    timeout_seconds=timeout_s,
                    memory_mb=ctx.memory_limit_mb,
                )
            )
            total_ms += run.duration_ms
            peak_kb = max(peak_kb, run.peak_memory_kb)

            if run.outcome is ExecOutcome.OK:
                actual = self.normalise(run.stdout)
                expected = self.normalise(case.expected_stdout)
                if actual == expected:
                    outcome = TestCaseOutcome.PASSED
                    earned += case.weight
                else:
                    outcome = TestCaseOutcome.WRONG_ANSWER
            else:
                outcome = _OUTCOME_MAP.get(run.outcome, TestCaseOutcome.WRONG_ANSWER)

            visible = case.is_sample
            results.append(
                CaseResult(
                    ordinal=case.ordinal,
                    test_case_id=case.id,
                    outcome=outcome,
                    execution_ms=run.duration_ms,
                    memory_kb=run.peak_memory_kb,
                    exit_code=run.exit_code,
                    # Hidden-case output is never stored. Returning it through
                    # the results API hands the candidate the answer key.
                    stdout=run.stdout if visible else "",
                    stderr=run.stderr if visible else "",
                    is_visible=visible,
                )
            )

            if outcome is TestCaseOutcome.COMPILE_ERROR:
                compile_output = run.stderr
                compile_failed = True

        if compile_failed:
            earned = 0.0

        score = round(100.0 * earned / total_weight, 2)
        return EvaluationResult(
            score=score,
            max_score=100.0,
            passed=bool(results) and all(r.passed for r in results),
            cases=results,
            compile_output=compile_output,
            execution_ms=total_ms,
            peak_memory_kb=peak_kb,
        )
