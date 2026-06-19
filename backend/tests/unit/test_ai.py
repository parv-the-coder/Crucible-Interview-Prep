"""AI layer behaviour.

Runs entirely against the fake provider, so the suite needs no API key, no
network, and costs nothing. That is the main reason the provider is behind an
interface at all.
"""

from __future__ import annotations

import pytest

from crucible.ai import AIRequest, AIUnavailableError, build_provider, prompt_fingerprint
from crucible.ai.base import AIProvider
from crucible.ai.fake import FakeProvider
from crucible.ai.prompts import (
    CODE_REVIEW_SCHEMA,
    code_review_prompt,
    debrief_prompt,
    follow_up_prompt,
    hint_prompt,
)
from crucible.db.enums import AIPurpose


def review_request(**kw) -> AIRequest:
    base = dict(
        prompt="review this code",
        purpose=AIPurpose.CODE_REVIEW,
        schema=CODE_REVIEW_SCHEMA,
    )
    base.update(kw)
    return AIRequest(**base)


# ------------------------------------------------------------- providers ---


def test_fake_provider_returns_schema_shaped_data() -> None:
    response = FakeProvider().complete(review_request())
    assert response.ok
    for key in ("summary", "complexity", "rubric", "strengths", "improvements"):
        assert key in response.data


def test_fake_provider_is_deterministic() -> None:
    """Otherwise every test asserting on AI output becomes flaky."""
    first = FakeProvider().complete(review_request())
    second = FakeProvider().complete(review_request())
    assert first.data == second.data


def test_fake_provider_can_simulate_an_outage() -> None:
    with pytest.raises(AIUnavailableError):
        FakeProvider(fail=True).complete(review_request())


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(AIUnavailableError, match="unknown AI provider"):
        build_provider("telepathy")


def test_gemini_without_a_key_explains_the_alternatives() -> None:
    """The error has to tell you what to do, not just that it failed."""
    from crucible.core.config import settings

    if settings.gemini_api_key.get_secret_value():
        pytest.skip("a real key is configured")
    with pytest.raises(AIUnavailableError) as exc:
        build_provider("gemini")
    message = str(exc.value)
    assert "GEMINI_API_KEY" in message
    assert "ollama" in message and "fake" in message


# ---------------------------------------------------------- fingerprints ---


def test_same_request_fingerprints_the_same() -> None:
    assert prompt_fingerprint(review_request(), "m1") == prompt_fingerprint(review_request(), "m1")


def test_a_different_model_is_a_different_request() -> None:
    """Otherwise a cached answer from one model is served for another."""
    assert prompt_fingerprint(review_request(), "m1") != prompt_fingerprint(review_request(), "m2")


def test_a_different_output_schema_is_a_different_request() -> None:
    plain = prompt_fingerprint(review_request(schema=None), "m1")
    structured = prompt_fingerprint(review_request(), "m1")
    assert plain != structured


def test_a_different_prompt_is_a_different_request() -> None:
    assert prompt_fingerprint(review_request(prompt="a"), "m1") != prompt_fingerprint(
        review_request(prompt="b"), "m1"
    )


# --------------------------------------------------------------- prompts ---


def test_review_prompt_states_the_test_verdict() -> None:
    """The model must not contradict the deterministic result."""
    passed = code_review_prompt(
        title="Two Sum",
        prompt_md="Find two numbers.",
        language="python",
        source="print(1)",
        passed=True,
        cases_passed=5,
        cases_total=5,
        failing_outcomes=[],
    )
    assert "All 5 tests passed" in passed

    failed = code_review_prompt(
        title="Two Sum",
        prompt_md="Find two numbers.",
        language="python",
        source="print(1)",
        passed=False,
        cases_passed=2,
        cases_total=5,
        failing_outcomes=["timeout"],
    )
    assert "2 of 5" in failed
    assert "timeout" in failed


def test_review_prompt_never_includes_the_answer_key() -> None:
    """The model grading against a solution the candidate cannot see is unfair,
    and a leaked hint would be worth more than the feedback."""
    prompt = code_review_prompt(
        title="Two Sum",
        prompt_md="Find two numbers that add to the target.",
        language="python",
        source="print(1)",
        passed=True,
        cases_passed=5,
        cases_total=5,
        failing_outcomes=[],
    )
    assert "reference" not in prompt.lower()
    assert "expected_stdout" not in prompt


def test_hint_prompt_distinguishes_an_empty_editor() -> None:
    empty = hint_prompt(title="T", prompt_md="P", attempt="   ", language="python")
    started = hint_prompt(title="T", prompt_md="P", attempt="x = 1", language="python")
    assert "not written anything" in empty
    assert "x = 1" in started


def test_follow_up_prompt_includes_the_solution() -> None:
    prompt = follow_up_prompt(title="Two Sum", language="python", source="print(1)")
    assert "print(1)" in prompt


def test_debrief_prompt_lists_every_topic() -> None:
    prompt = debrief_prompt(
        percentage=62.5,
        per_topic={
            "arrays": {"percentage": 90.0, "count": 2},
            "sql": {"percentage": 35.0, "count": 1},
        },
    )
    assert "62.5%" in prompt
    assert "arrays" in prompt and "sql" in prompt


# --------------------------------------------------------------- contract ---


def test_every_provider_implements_the_interface() -> None:
    from crucible.ai.gemini import GeminiProvider
    from crucible.ai.ollama import OllamaProvider

    for cls in (FakeProvider, OllamaProvider, GeminiProvider):
        assert issubclass(cls, AIProvider)
        assert callable(cls.complete)


def test_cost_estimate_is_zero_when_no_pricing_is_set() -> None:
    assert FakeProvider().estimate_cost(1000, 1000) == 0.0


def test_cost_estimate_scales_with_tokens() -> None:
    class Priced(FakeProvider):
        price_per_mtok_in = 1.0
        price_per_mtok_out = 2.0

    provider = Priced()
    assert provider.estimate_cost(1_000_000, 0) == 1.0
    assert provider.estimate_cost(0, 1_000_000) == 2.0
    assert provider.estimate_cost(1_000_000, 1_000_000) == 3.0
