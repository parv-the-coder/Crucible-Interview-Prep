"""Liveness and readiness.

The distinction matters to an orchestrator: liveness failing means restart me;
readiness failing means stop sending traffic but do not restart. Conflating
them produces restart loops during a database blip.
"""

from __future__ import annotations

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


@router.get("/health/live", summary="Liveness probe")
async def live() -> dict[str, str]:
    """Is the process running? Nothing else. Never touches a dependency."""
    return {"status": "ok"}


@router.get("/health/ready", response_model=Health, summary="Readiness probe")
async def ready(response: Response) -> Health:
    db_check = await _check_database()
    checks = {"database": db_check}

    # Postgres being down means we can neither accept work nor run it, since
    # the queue lives there too.
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
