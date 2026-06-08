"""Structured logging with request-scoped correlation IDs.

Every log line emitted while handling a request carries `request_id`, and
`user_id`/`session_id` once known. That is what makes a production incident
searchable: one grep pins the whole causal chain across API and worker.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

from crucible.core.config import settings

# Context propagates automatically through async tasks -- no manual threading.
request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_ctx: ContextVar[str | None] = ContextVar("user_id", default=None)
session_id_ctx: ContextVar[str | None] = ContextVar("session_id", default=None)


def _inject_context(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key, ctx in (
        ("request_id", request_id_ctx),
        ("user_id", user_id_ctx),
        ("session_id", session_id_ctx),
    ):
        value = ctx.get()
        if value is not None:
            event_dict.setdefault(key, value)
    return event_dict


def configure_logging() -> None:
    """Idempotently configure stdlib logging + structlog."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _inject_context,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            *shared,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    # uvicorn duplicates access logs we already emit in middleware.
    logging.getLogger("uvicorn.access").disabled = True
    for noisy in ("asyncio", "aiormq", "docker", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
