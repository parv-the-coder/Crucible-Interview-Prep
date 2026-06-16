"""Evaluation strategy behaviour."""

from __future__ import annotations

import uuid

import pytest

from crucible.db.enums import TestCaseOutcome
from crucible.evaluation.strategies import UnsupportedQuestionTypeError, build_strategy
from crucible.evaluation.strategies.base import EvaluationContext, EvaluationStrategy, TestCaseSpec


def ctx(**kw) -> EvaluationContext:
    base = dict(
        submission_id=uuid.uuid4(),
        question_id=uuid.uuid4(),
        question_type="code",
        language="python",
        source_code="",
        answer={},
        payload={},
        test_cases=(),
        time_limit_ms=3000,
        memory_limit_mb=256,
    )
    base.update(kw)
    return EvaluationContext(**base)


def case(ordinal: int, stdin: str, expected: str, sample: bool = False, weight: float = 1.0):
    return TestCaseSpec(
        id=uuid.uuid4(),
        ordinal=ordinal,
        stdin=stdin,
        expected_stdout=expected,
        is_sample=sample,
        weight=weight,
    )


# ------------------------------------------------------------ normalising ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("42\n", "42"),
        ("42\r\n", "42"),
        ("  42  \n\n", "42"),
        ("a \nb\t\n", "a\nb"),
        ("", ""),
    ],
)
def test_output_normalisation_ignores_incidental_whitespace(raw: str, expected: str) -> None:
    """CRLF is not a wrong answer. v1 failed Windows users for their line endings."""
    assert EvaluationStrategy.normalise(raw) == expected


# ------------------------------------------------------------------ code ---


def test_code_all_cases_passing_scores_100(sandbox) -> None:
    strategy = build_strategy("code", sandbox=sandbox)
    source = "import sys\nprint(sum(int(x) for x in sys.stdin.read().split()))"
    result = strategy.evaluate(
        ctx(
            source_code=source,
            test_cases=(case(0, "1 2", "3", sample=True), case(1, "10 20", "30")),
        )
    )
    assert result.passed
    assert result.score == 100.0
    assert result.cases_passed == 2


def test_code_partial_pass_is_weighted(sandbox) -> None:
    strategy = build_strategy("code", sandbox=sandbox)
    source = "import sys\nprint(42)"  # right for one case, wrong for the other
    result = strategy.evaluate(
        ctx(source_code=source, test_cases=(case(0, "", "42"), case(1, "", "99")))
    )
    assert not result.passed
    assert result.score == 50.0
    assert result.cases[1].outcome is TestCaseOutcome.WRONG_ANSWER


def test_code_hidden_case_output_is_never_returned(sandbox) -> None:
    """Returning hidden stdout hands the candidate the answer key."""
    strategy = build_strategy("code", sandbox=sandbox)
    result = strategy.evaluate(
        ctx(
            source_code="print('SECRET-VALUE')",
            test_cases=(case(0, "", "SECRET-VALUE", sample=False),),
        )
    )
    assert result.cases[0].is_visible is False
    assert result.cases[0].stdout == ""


def test_code_sample_case_output_is_returned(sandbox) -> None:
    strategy = build_strategy("code", sandbox=sandbox)
    result = strategy.evaluate(
        ctx(source_code="print('visible')", test_cases=(case(0, "", "visible", sample=True),))
    )
    assert result.cases[0].is_visible is True
    assert "visible" in result.cases[0].stdout


def test_code_timeout_is_not_reported_as_a_wrong_answer(sandbox) -> None:
    strategy = build_strategy("code", sandbox=sandbox)
    result = strategy.evaluate(
        ctx(source_code="while True: pass", time_limit_ms=1000, test_cases=(case(0, "", "x"),))
    )
    assert result.cases[0].outcome is TestCaseOutcome.TIMEOUT


def test_code_dry_run_only_executes_sample_cases(sandbox) -> None:
    strategy = build_strategy("code", sandbox=sandbox)
    cases = (case(0, "", "1", sample=True), case(1, "", "2"), case(2, "", "3"))
    result = strategy.evaluate(ctx(source_code="print(1)", test_cases=cases, is_dry_run=True))
    assert result.cases_total == 1


def test_code_empty_submission_is_rejected_without_touching_the_sandbox(sandbox) -> None:
    strategy = build_strategy("code", sandbox=sandbox)
    result = strategy.evaluate(ctx(source_code="   ", test_cases=(case(0, "", "x"),)))
    assert result.score == 0.0
    assert "Empty" in result.error_message


# ------------------------------------------------------------------- mcq ---


def test_mcq_exact_match_scores_full() -> None:
    strategy = build_strategy("mcq")
    result = strategy.evaluate(
        ctx(question_type="mcq", answer={"selected": ["b"]}, payload={"correct": ["b"]})
    )
    assert result.passed and result.score == 100.0


def test_mcq_is_case_insensitive() -> None:
    strategy = build_strategy("mcq")
    result = strategy.evaluate(
        ctx(question_type="mcq", answer={"selected": ["B"]}, payload={"correct": ["b"]})
    )
    assert result.passed


def test_mcq_accepts_a_bare_string_answer() -> None:
    strategy = build_strategy("mcq")
    result = strategy.evaluate(
        ctx(question_type="mcq", answer={"selected": "b"}, payload={"correct": ["b"]})
    )
    assert result.passed


def test_mcq_multi_answer_partial_credit() -> None:
    strategy = build_strategy("mcq")
    result = strategy.evaluate(
        ctx(
            question_type="mcq",
            answer={"selected": ["a", "b"]},
            payload={"correct": ["a", "b", "c"]},
        )
    )
    assert not result.passed
    assert result.score == pytest.approx(66.67, abs=0.01)


def test_mcq_selecting_everything_does_not_score_full() -> None:
    """Without negative marking, 'select all' is a 100% strategy."""
    strategy = build_strategy("mcq")
    result = strategy.evaluate(
        ctx(
            question_type="mcq",
            answer={"selected": ["a", "b", "c", "d"]},
            payload={"correct": ["a", "b"]},
        )
    )
    assert result.score == 0.0


def test_mcq_without_an_answer_key_fails_loudly() -> None:
    strategy = build_strategy("mcq")
    result = strategy.evaluate(ctx(question_type="mcq", answer={"selected": ["a"]}, payload={}))
    assert "answer key" in result.error_message


# ------------------------------------------------------------------- sql ---

SCHEMA = "CREATE TABLE employees (id INTEGER, name TEXT, dept TEXT, salary INTEGER);"
SEED = """
INSERT INTO employees VALUES (1,'Ana','eng',120);
INSERT INTO employees VALUES (2,'Bo','eng',100);
INSERT INTO employees VALUES (3,'Cy','sales',90);
"""


def sql_ctx(query: str, expected, **extra):
    payload = {"schema_sql": SCHEMA, "seed_sql": SEED, "expected_rows": expected}
    payload.update(extra)
    return ctx(question_type="sql", language="sql", source_code=query, payload=payload)


def test_sql_correct_query_passes() -> None:
    strategy = build_strategy("sql")
    result = strategy.evaluate(
        sql_ctx("SELECT name FROM employees WHERE dept='eng' ORDER BY name", [["Ana"], ["Bo"]])
    )
    assert result.passed and result.score == 100.0


def test_sql_row_order_is_ignored_without_order_by() -> None:
    """SQL result order is undefined without ORDER BY; failing on it is wrong."""
    strategy = build_strategy("sql")
    result = strategy.evaluate(
        sql_ctx("SELECT name FROM employees WHERE dept='eng'", [["Bo"], ["Ana"]])
    )
    assert result.passed


def test_sql_integer_and_float_compare_equal() -> None:
    strategy = build_strategy("sql")
    result = strategy.evaluate(
        sql_ctx("SELECT AVG(salary) FROM employees WHERE dept='eng'", [[110]])
    )
    assert result.passed


def test_sql_wrong_result_fails_with_a_row_count_hint() -> None:
    strategy = build_strategy("sql")
    result = strategy.evaluate(sql_ctx("SELECT name FROM employees", [["Ana"]]))
    assert not result.passed
    assert "expected 1" in result.cases[0].stderr


def test_sql_mutating_statement_is_refused() -> None:
    strategy = build_strategy("sql")
    result = strategy.evaluate(sql_ctx("DROP TABLE employees", [["Ana"]]))
    assert not result.passed
    assert "SELECT" in result.error_message


def test_sql_syntax_error_is_a_runtime_error_not_a_crash() -> None:
    strategy = build_strategy("sql")
    result = strategy.evaluate(sql_ctx("SELEKT * FROM employees", [["Ana"]]))
    assert result.cases[0].outcome is TestCaseOutcome.RUNTIME_ERROR
    assert "SQL error" in result.error_message


def test_sql_empty_query_is_rejected() -> None:
    strategy = build_strategy("sql")
    result = strategy.evaluate(sql_ctx("   ", [["Ana"]]))
    assert "Empty" in result.error_message


# -------------------------------------------------------------- registry ---


def test_unknown_question_type_raises() -> None:
    with pytest.raises(UnsupportedQuestionTypeError):
        build_strategy("telepathy")
