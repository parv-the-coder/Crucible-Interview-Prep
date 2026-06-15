"""Liveness and readiness.

The distinction matters to an orchestrator: liveness failing means restart me;
readiness failing means stop sending traffic but do not restart. Conflating
them produces restart loops during a database blip.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from crucible.core.config import settings
from crucible.db.session import get_async_engine
from crucible.schemas.common import Health

router = APIRouter(tags=["health"])

VERSION = "0.1.0"


async def _check_database() -> dict[str, Any]:
    started = time.perf_counter()
    try:
        engine = get_async_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


async def _check_sandbox() -> dict[str, Any]:
    """Report what isolation is actually in force.

    A deployment running the insecure fallback must not be able to look
    healthy. This is the line that makes that visible.
    """
    try:
        from crucible.evaluation.sandbox import get_sandbox

        sandbox = await asyncio.to_thread(get_sandbox)
        caps = sandbox.capabilities
        healthy = await asyncio.to_thread(sandbox.healthy)
        return {
            "ok": healthy,
            "backend": caps.name,
            "production_safe": caps.production_safe,
            "warnings": caps.notes if not caps.production_safe else [],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/health/live", summary="Liveness probe")
async def live() -> dict[str, str]:
    """Is the process running? Nothing else. Never touches a dependency."""
    return {"status": "ok"}


@router.get("/health/ready", response_model=Health, summary="Readiness probe")
async def ready(response: Response) -> Health:
    db_check, sandbox_check = await asyncio.gather(_check_database(), _check_sandbox())
    checks = {"database": db_check, "sandbox": sandbox_check}

    # The sandbox being insecure is a warning, not an outage: the platform
    # still serves. Postgres being down means we can neither accept work nor
    # run it, since the queue lives there too.
    ready_now = bool(db_check["ok"])
    if not ready_now:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return Health(
        status="ready" if ready_now else "not_ready",
        version=VERSION,
        environment=settings.environment,
        checks=checks,
    )


@router.get("/health", response_model=Health, summary="Full health snapshot")
async def health(response: Response) -> Health:
    return await ready(response)
