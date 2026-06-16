"""Grade a SQL answer by executing it against a throwaway SQLite database.

v1 used alasql, a JavaScript in-memory engine, and compared normalised CSV
strings. Two problems: alasql's dialect diverges from anything a candidate will
meet in an interview, and string comparison makes column order and formatting
part of the answer.

Here each submission gets its own in-memory SQLite database, seeded from the
question, and results are compared as row multisets. SQLite is not Postgres,
but it is a real SQL engine with a real query planner, and it costs nothing to
create and discard per submission.

The query is executed read-only: a candidate answering DROP TABLE gets an
error, not a mutated fixture that breaks the next submission.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from crucible.db.enums import TestCaseOutcome
from crucible.evaluation.strategies.base import (
    CaseResult,
    EvaluationContext,
    EvaluationResult,
    EvaluationStrategy,
)

# Statements that mutate. Answering a SELECT question with any of these is a
# wrong answer, and letting them run would corrupt the fixture mid-grading.
_MUTATING = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach|detach|pragma|vacuum)\b",
    re.IGNORECASE,
)


class SqlStrategy(EvaluationStrategy):
    question_type = "sql"

    def evaluate(self, ctx: EvaluationContext) -> EvaluationResult:
        query = ctx.source_code.strip() or str(ctx.answer.get("query", "")).strip()
        if not query:
            return EvaluationResult(
                score=0.0, max_score=100.0, passed=False, error_message="Empty query"
            )

        schema_sql = str(ctx.payload.get("schema_sql", "")).strip()
        seed_sql = str(ctx.payload.get("seed_sql", "")).strip()
        expected_rows = ctx.payload.get("expected_rows")
        if expected_rows is None:
            return EvaluationResult(
                score=0.0,
                max_score=100.0,
                passed=False,
                error_message="Question has no expected result set",
            )

        if _MUTATING.search(query):
            return self._fail(
                "Only SELECT statements are accepted for this question.",
                TestCaseOutcome.WRONG_ANSWER,
            )

        try:
            rows, columns = self._run(schema_sql, seed_sql, query)
        except sqlite3.Error as exc:
            return self._fail(f"SQL error: {exc}", TestCaseOutcome.RUNTIME_ERROR)

        ordered = bool(ctx.payload.get("order_matters", "order by" in query.lower()))
        expected = [tuple(r) for r in (tuple(x) for x in expected_rows)]
        actual = [tuple(r) for r in rows]

        if ordered:
            passed = self._canon(actual) == self._canon(expected)
        else:
            # Without ORDER BY, SQL result order is undefined. Comparing as an
            # ordered list would fail correct answers for no reason.
            passed = sorted(self._canon(actual)) == sorted(self._canon(expected))

        detail = (
            f"returned {len(actual)} row(s), expected {len(expected)}"
            if not passed
            else f"{len(actual)} row(s)"
        )
        return EvaluationResult(
            score=100.0 if passed else 0.0,
            max_score=100.0,
            passed=passed,
            cases=[
                CaseResult(
                    ordinal=0,
                    outcome=TestCaseOutcome.PASSED if passed else TestCaseOutcome.WRONG_ANSWER,
                    stdout=self._preview(columns, actual),
                    stderr="" if passed else detail,
                    is_visible=True,
                )
            ],
        )

    # ---------------------------------------------------------- internals --

    @staticmethod
    def _run(schema_sql: str, seed_sql: str, query: str) -> tuple[list[Any], list[str]]:
        conn = sqlite3.connect(":memory:")
        try:
            if schema_sql:
                conn.executescript(schema_sql)
            if seed_sql:
                conn.executescript(seed_sql)
            conn.commit()

            # Read-only from here: the authorizer rejects any write attempt at
            # the engine level, so a mutation that slipped past the regex still
            # cannot touch the fixture.
            conn.set_authorizer(_read_only_authorizer)
            cursor = conn.execute(query)
            rows = cursor.fetchmany(1000)
            columns = [d[0] for d in (cursor.description or [])]
            return rows, columns
        finally:
            conn.close()

    @staticmethod
    def _canon(rows: list[tuple[Any, ...]]) -> list[tuple[str, ...]]:
        """Compare values, not their Python types.

        1 and 1.0 and "1" are the same answer; a candidate should not fail
        because SQLite inferred a different storage class than the fixture.
        """

        def cell(v: Any) -> str:
            if v is None:
                return "\x00NULL"
            if isinstance(v, float) and v.is_integer():
                return str(int(v))
            if isinstance(v, bool):
                return str(int(v))
            return str(v).strip()

        return [tuple(cell(v) for v in row) for row in rows]

    @staticmethod
    def _preview(columns: list[str], rows: list[tuple[Any, ...]], limit: int = 20) -> str:
        header = " | ".join(columns) if columns else ""
        body = "\n".join(
            " | ".join("NULL" if v is None else str(v) for v in r) for r in rows[:limit]
        )
        more = f"\n... {len(rows) - limit} more row(s)" if len(rows) > limit else ""
        return f"{header}\n{body}{more}".strip()

    @staticmethod
    def _fail(message: str, outcome: TestCaseOutcome) -> EvaluationResult:
        return EvaluationResult(
            score=0.0,
            max_score=100.0,
            passed=False,
            error_message=message,
            cases=[CaseResult(ordinal=0, outcome=outcome, stderr=message, is_visible=True)],
        )


def _read_only_authorizer(action: int, *_args: Any) -> int:
    """SQLite authorizer callback: permit reads, deny everything else."""
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY
