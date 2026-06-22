"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from crucible.api import ws
from crucible.api.errors import register_exception_handlers
from crucible.api.middleware import RequestContextMiddleware
from crucible.api.v1 import auth, health, questions, sessions, submissions
from crucible.core.config import settings
from crucible.core.logging import configure_logging, get_logger

log = get_logger(__name__)

DESCRIPTION = """
AI-assisted technical interview platform.

* **Sandboxed execution** - candidate code runs in hardened containers.
* **Async grading** - submissions are queued; results arrive over WebSocket.
* **AI review** - rubric-based feedback and adaptive follow-up questions.
* **Live rooms** - collaborative editing for real interviews.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    log.info(
        "app.starting",
        environment=settings.environment,
        sandbox_backend=settings.sandbox_backend,
        ai_provider=settings.ai_provider if settings.ai_enabled else "disabled",
    )

    # Room connection registry. Fan-out is process-local; see realtime/hub.py.
    from crucible.realtime.hub import hub

    await hub.start()

    yield

    await hub.stop()

    log.info("app.stopping")
    # Dispose the pool explicitly: leaving sockets to the GC produces
    # "connection was closed in the middle of operation" noise on shutdown.
    from crucible.db.session import get_async_engine

    await get_async_engine().dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
        # Interactive docs are a reconnaissance aid in production.
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url="/redoc" if settings.environment != "production" else None,
        openapi_url="/openapi.json" if settings.environment != "production" else None,
    )

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(auth.router, prefix=settings.api_v1_prefix)
    app.include_router(questions.router, prefix=settings.api_v1_prefix)
    app.include_router(sessions.router, prefix=settings.api_v1_prefix)
    app.include_router(submissions.router, prefix=settings.api_v1_prefix)
    # WebSockets are not versioned under /api/v1: the protocol is negotiated
    # on the frame, not the path.
    app.include_router(ws.router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": settings.app_name,
            "version": "0.1.0",
            "docs": "/docs",
            "health": "/health/ready",
        }

    return app


app = create_app()
