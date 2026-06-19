"""The layer between services and a provider.

Everything cross-cutting lives here rather than being repeated at each call
site: budgets, caching, the audit ledger, schema validation, and degradation
when the provider is down.

The rule that matters most: **AI output is never in the scoring path.** Test
cases decide the score. If every call in this module failed, submissions would
still be graded correctly and the only loss would be the explanation.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from crucible.ai.base import (
    AIBudgetExceededError,
    AIError,
    AIProvider,
    AIRequest,
    AIResponse,
    AIUnavailableError,
)
from crucible.core.config import settings
from crucible.core.logging import get_logger
from crucible.db.models import AIInteraction

log = get_logger(__name__)

_provider: AIProvider | None = None


def build_provider(name: str | None = None) -> AIProvider:
    """Construct a provider. Never touches the process-wide singleton."""
    key = (name or settings.ai_provider).lower()

    if key == "fake":
        from crucible.ai.fake import FakeProvider

        return FakeProvider()
    if key == "ollama":
        from crucible.ai.ollama import OllamaProvider

        return OllamaProvider()
    if key == "gemini":
        from crucible.ai.gemini import GeminiProvider

        return GeminiProvider()
    raise AIUnavailableError(f"unknown AI provider: {key!r}")


def get_provider() -> AIProvider:
    """Process-wide singleton.

    Providers hold an HTTP client and connection pool; one per worker process
    rather than one per task.
    """
    global _provider
    if _provider is None:
        _provider = build_provider()
    return _provider


def reset_provider() -> None:
    global _provider
    _provider = None


def prompt_fingerprint(request: AIRequest, model: str) -> str:
    """Cache key.

    Includes the model and the schema, not just the prompt: the same words sent
    to a different model, or with a different output shape, is a different
    request and must not return a cached answer from the other.
    """
    material = json.dumps(
        {
            "prompt": request.prompt,
            "system": request.system,
            "schema": request.schema,
            "purpose": request.purpose.value,
            "model": model,
            "temperature": request.temperature,
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _budget_used_today(db: Session, user_id: Any) -> int:
    """Billable calls this user has made since midnight UTC.

    Counts rows in the ledger rather than keeping a counter, so the number
    cannot drift from what actually happened. Cached and failed calls are
    excluded because neither costs anything.
    """
    since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        db.execute(
            select(func.count(AIInteraction.id)).where(
                AIInteraction.user_id == user_id,
                AIInteraction.created_at >= since,
                AIInteraction.cached.is_(False),
                AIInteraction.ok.is_(True),
            )
        ).scalar_one()
    )


def _cached_response(db: Session, fingerprint: str) -> AIInteraction | None:
    """A successful identical call from the last day.

    Regrading the same submission, or two candidates submitting byte-identical
    code, should not be paid for twice. TTL is a day because prompts here are
    deterministic given the same inputs.
    """
    cutoff = datetime.now(UTC) - timedelta(days=1)
    return db.execute(
        select(AIInteraction)
        .where(
            AIInteraction.prompt_hash == fingerprint,
            AIInteraction.ok.is_(True),
            AIInteraction.created_at >= cutoff,
        )
        .order_by(AIInteraction.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def complete(db: Session, request: AIRequest, *, provider: AIProvider | None = None) -> AIResponse:
    """Run one AI call with budget, cache and audit around it.

    Raises AIError on failure. Callers are expected to catch it and carry on
    without the feedback.
    """
    if not settings.ai_enabled:
        raise AIUnavailableError("AI features are disabled")

    active = provider or get_provider()
    fingerprint = prompt_fingerprint(request, active.model)

    # --- cache ------------------------------------------------------------
    hit = _cached_response(db, fingerprint)
    if hit is not None:
        db.add(
            AIInteraction(
                user_id=request.user_id,
                submission_id=request.submission_id,
                session_id=request.session_id,
                room_id=request.room_id,
                purpose=request.purpose,
                provider=active.name,
                model=active.model,
                prompt_hash=fingerprint,
                prompt="",  # already stored on the original row
                response=hit.response,
                cached=True,
                ok=True,
            )
        )
        return AIResponse(
            text=json.dumps(hit.response) if hit.response else "",
            data=hit.response or {},
            provider=active.name,
            model=active.model,
            cached=True,
        )

    # --- budget -----------------------------------------------------------
    if request.user_id is not None:
        used = _budget_used_today(db, request.user_id)
        if used >= settings.ai_daily_budget_per_user:
            raise AIBudgetExceededError(
                f"Daily AI limit reached ({used}/{settings.ai_daily_budget_per_user})"
            )

    # --- call -------------------------------------------------------------
    try:
        response = active.complete(request)
    except AIError as exc:
        db.add(
            AIInteraction(
                user_id=request.user_id,
                submission_id=request.submission_id,
                session_id=request.session_id,
                room_id=request.room_id,
                purpose=request.purpose,
                provider=active.name,
                model=active.model,
                prompt_hash=fingerprint,
                prompt=request.prompt[:8000],
                ok=False,
                error=str(exc)[:2000],
            )
        )
        log.warning(
            "ai.call_failed",
            provider=active.name,
            purpose=request.purpose.value,
            error=str(exc)[:200],
        )
        raise

    # --- record -----------------------------------------------------------
    db.add(
        AIInteraction(
            user_id=request.user_id,
            submission_id=request.submission_id,
            session_id=request.session_id,
            room_id=request.room_id,
            purpose=request.purpose,
            provider=active.name,
            model=active.model,
            prompt_hash=fingerprint,
            # Truncated: prompts embed the candidate's source, and the ledger
            # should not become a second copy of every submission.
            prompt=request.prompt[:8000],
            response=response.data or {"text": response.text[:8000]},
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            latency_ms=response.latency_ms,
            estimated_cost_usd=active.estimate_cost(
                response.prompt_tokens, response.completion_tokens
            ),
            ok=True,
        )
    )

    log.info(
        "ai.completed",
        provider=active.name,
        purpose=request.purpose.value,
        tokens=response.total_tokens,
        latency_ms=response.latency_ms,
    )
    return response


def complete_or_none(
    db: Session, request: AIRequest, *, provider: AIProvider | None = None
) -> AIResponse | None:
    """complete() that degrades instead of raising.

    This is what services actually call. An LLM being down, rate-limited or
    over budget must never turn into a failed submission or a 500 on a page
    the candidate needs.
    """
    try:
        return complete(db, request, provider=provider)
    except AIError:
        return None
