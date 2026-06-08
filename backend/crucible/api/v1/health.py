"""Liveness and readiness.

The distinction matters to an orchestrator: liveness failing means restart me;
readiness failing means stop sending traffic but do not restart. Conflating
them produces restart loops during a database blip.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from crucible.core.config import settings
from crucible.schemas.common import Health

router = APIRouter(tags=["health"])

VERSION = "0.1.0"


@router.get("/health/live", summary="Liveness probe")
async def live() -> dict[str, str]:
    """Is the process running? Nothing else. Never touches a dependency."""
    return {"status": "ok"}


@router.get("/health/ready", response_model=Health, summary="Readiness probe")
async def ready(response: Response) -> Health:
    # Dependency checks land as the dependencies do.
    checks: dict[str, dict[str, Any]] = {}
    return Health(
        status="ready",
        version=VERSION,
        environment=settings.environment,
        checks=checks,
    )
