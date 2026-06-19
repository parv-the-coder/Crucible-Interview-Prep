"""Deterministic provider for tests and offline development.

Returns plausible, schema-valid output without a network call, so the entire AI
layer can be exercised in CI for free. Deterministic by prompt hash, so a test
asserting on output does not become flaky.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from crucible.ai.base import AIProvider, AIRequest, AIResponse
from crucible.db.enums import AIPurpose


class FakeProvider(AIProvider):
    name = "fake"
    model = "fake-1"

    def __init__(self, *, fail: bool = False) -> None:
        # Lets a test drive the degradation path deliberately.
        self._fail = fail

    def complete(self, request: AIRequest) -> AIResponse:
        if self._fail:
            from crucible.ai.base import AIUnavailableError

            raise AIUnavailableError("fake provider configured to fail")

        seed = int(hashlib.sha256(request.prompt.encode()).hexdigest()[:8], 16)
        data = self._payload(request.purpose, seed)
        text = json.dumps(data) if data else "Fake response."

        return AIResponse(
            text=text,
            data=data,
            provider=self.name,
            model=self.model,
            prompt_tokens=max(1, len(request.prompt) // 4),
            completion_tokens=max(1, len(text) // 4),
            latency_ms=1,
        )

    @staticmethod
    def _payload(purpose: AIPurpose, seed: int) -> dict[str, Any]:
        if purpose is AIPurpose.CODE_REVIEW:
            return {
                "summary": "The approach is reasonable and the logic is correct.",
                "correctness": "Handles the sample cases; check empty input.",
                "complexity": {"time": "O(n)", "space": "O(n)"},
                "strengths": ["Clear variable names", "Single pass over the input"],
                "improvements": ["Add a guard for empty input"],
                "rubric": {
                    "correctness": 4 + seed % 2,
                    "efficiency": 3 + seed % 3,
                    "readability": 4,
                },
            }
        if purpose is AIPurpose.FOLLOW_UP:
            return {
                "question": "How would your solution change if the input did not fit in memory?",
                "why": "Checks whether the candidate can reason beyond the given constraints.",
            }
        if purpose is AIPurpose.HINT:
            return {"hint": "Think about what you would store to avoid the inner loop."}
        if purpose is AIPurpose.SESSION_DEBRIEF:
            return {
                "summary": "Solid on arrays, weaker on SQL.",
                "focus_next": ["sql", "sliding-window"],
            }
        return {}
