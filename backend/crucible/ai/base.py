"""LLM provider contract.

Services depend on this, never on a vendor SDK. Two reasons: model names and
pricing change every few months and should not edit business logic, and a fake
implementation keeps the whole AI layer testable offline and free.

See docs/adr/0008-provider-agnostic-ai.md.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from crucible.db.enums import AIPurpose


class AIError(Exception):
    """Provider failure. Callers degrade; they never propagate this to a user."""


class AIUnavailableError(AIError):
    """No provider configured, or the provider is unreachable."""


class AIBudgetExceededError(AIError):
    """This user has used their allowance for today."""


@dataclass(frozen=True, slots=True)
class AIRequest:
    prompt: str
    purpose: AIPurpose
    system: str | None = None
    # JSON schema the response must satisfy. When set, the provider is asked
    # for structured output and the result is validated before returning.
    schema: dict[str, Any] | None = None
    max_output_tokens: int | None = None
    temperature: float = 0.2
    # Identifies the caller for the audit ledger and per-user budgets.
    user_id: Any = None
    submission_id: Any = None
    session_id: Any = None
    room_id: Any = None


@dataclass(slots=True)
class AIResponse:
    text: str
    data: dict[str, Any] = field(default_factory=dict)
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cached: bool = False
    ok: bool = True
    error: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class AIProvider(ABC):
    """One adapter per vendor. Deliberately narrow.

    Kept to a single method on purpose. A wider interface drifts into being a
    worse reimplementation of LangChain, and every extra method is one more
    thing each adapter has to get right.
    """

    name: str
    model: str

    @abstractmethod
    def complete(self, request: AIRequest) -> AIResponse:
        """Send a prompt and return the response.

        Must raise AIError (not a vendor exception) on failure, so callers can
        degrade without knowing which provider is configured.
        """

    def healthy(self) -> bool:
        return True

    # Rough USD per million tokens, used for the cost column in the ledger.
    # Approximate on purpose: the point is spotting a runaway loop, not
    # invoicing.
    price_per_mtok_in: float = 0.0
    price_per_mtok_out: float = 0.0

    def estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return round(
            prompt_tokens / 1_000_000 * self.price_per_mtok_in
            + completion_tokens / 1_000_000 * self.price_per_mtok_out,
            6,
        )
