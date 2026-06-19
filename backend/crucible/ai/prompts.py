"""Prompts and their response schemas.

Kept together and out of the service code, so a prompt change is a reviewable
diff in one file rather than a string edit buried in business logic.

Two rules apply to everything here:

1. **The model never sees the answer key.** Not the hidden test cases, not the
   reference solution. If it did, its feedback would be graded against a
   solution the candidate cannot see, and a leaked hint would be worth more
   than the feedback.
2. **The model never decides the score.** Test cases already did that. The
   model explains the result and rates *qualities* the tests cannot measure,
   like readability. Its rubric numbers are advisory and stored separately from
   the score.
"""

from __future__ import annotations

from typing import Any

CODE_REVIEW_SYSTEM = """You are a senior engineer reviewing a candidate's solution \
in a technical interview.

Be specific and brief. Point at actual lines and name actual issues; avoid \
generic advice like "add comments" or "consider edge cases" unless you can say \
which edge case.

You are told whether the tests passed. Do not contradict that verdict: if the \
tests passed, the solution is correct for the given cases, and your job is to \
comment on how it is written and how it would scale. If the tests failed, help \
the candidate see why without simply handing them the fix.

Never write the corrected solution."""

CODE_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Two sentences at most."},
        "correctness": {"type": "string"},
        "complexity": {
            "type": "object",
            "properties": {"time": {"type": "string"}, "space": {"type": "string"}},
            "required": ["time", "space"],
        },
        "strengths": {"type": "array", "items": {"type": "string"}},
        "improvements": {"type": "array", "items": {"type": "string"}},
        "rubric": {
            "type": "object",
            "properties": {
                "correctness": {"type": "integer"},
                "efficiency": {"type": "integer"},
                "readability": {"type": "integer"},
            },
            "required": ["correctness", "efficiency", "readability"],
        },
    },
    "required": ["summary", "correctness", "complexity", "strengths", "improvements", "rubric"],
}


def code_review_prompt(
    *,
    title: str,
    prompt_md: str,
    language: str,
    source: str,
    passed: bool,
    cases_passed: int,
    cases_total: int,
    failing_outcomes: list[str],
) -> str:
    verdict = (
        f"All {cases_total} tests passed."
        if passed
        else f"{cases_passed} of {cases_total} tests passed. "
        f"Failures: {', '.join(failing_outcomes) or 'wrong answer'}."
    )
    return f"""Question: {title}

{prompt_md}

Test result: {verdict}

The candidate's {language} solution:
```{language}
{source}
```

Review it. Rate correctness, efficiency and readability out of 5.
Correctness must agree with the test result above."""


FOLLOW_UP_SYSTEM = """You are an interviewer asking one follow-up question after a \
candidate solved a problem.

Ask what a good interviewer asks next: scale the input, change a constraint, or \
probe a trade-off they made implicitly. One question. No preamble."""

FOLLOW_UP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "why": {"type": "string", "description": "What this tests. One sentence."},
    },
    "required": ["question", "why"],
}


def follow_up_prompt(*, title: str, language: str, source: str) -> str:
    return f"""The candidate solved "{title}" in {language}:

```{language}
{source}
```

Ask one follow-up question."""


HINT_SYSTEM = """You are helping someone who is stuck.

Give one nudge toward the idea they are missing. Never give the solution, never \
give code, and never name the algorithm outright if that would end the problem."""

HINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"hint": {"type": "string"}},
    "required": ["hint"],
}


def hint_prompt(*, title: str, prompt_md: str, attempt: str, language: str) -> str:
    attempted = (
        f"Their current attempt:\n```{language}\n{attempt}\n```"
        if attempt.strip()
        else "They have not written anything yet."
    )
    return f"""Question: {title}

{prompt_md}

{attempted}

Give one hint."""


DEBRIEF_SYSTEM = """You are summarising a practice test for the candidate.

Be direct about what went badly. Encouragement that hides a weakness wastes \
their time."""

DEBRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "focus_next": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "focus_next"],
}


def debrief_prompt(*, percentage: float, per_topic: dict[str, dict[str, float]]) -> str:
    lines = "\n".join(
        f"- {topic}: {stats.get('percentage', 0)}% over {int(stats.get('count', 0))} question(s)"
        for topic, stats in sorted(per_topic.items())
    )
    return f"""The candidate scored {percentage}% overall.

By topic:
{lines}

Summarise in two sentences and name up to three topics to focus on next."""
