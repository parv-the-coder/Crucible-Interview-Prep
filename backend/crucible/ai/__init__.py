"""AI layer.

Import from here; the concrete providers are an implementation detail.
"""

from crucible.ai.base import (
    AIBudgetExceededError,
    AIError,
    AIProvider,
    AIRequest,
    AIResponse,
    AIUnavailableError,
)
from crucible.ai.client import (
    build_provider,
    complete,
    complete_or_none,
    get_provider,
    prompt_fingerprint,
    reset_provider,
)

__all__ = [
    "AIBudgetExceededError",
    "AIError",
    "AIProvider",
    "AIRequest",
    "AIResponse",
    "AIUnavailableError",
    "build_provider",
    "complete",
    "complete_or_none",
    "get_provider",
    "prompt_fingerprint",
    "reset_provider",
]
