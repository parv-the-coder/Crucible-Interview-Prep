"""HTTP middleware: correlation IDs and access logging."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from crucible.core.logging import get_logger, request_id_ctx, session_id_ctx, user_id_ctx

log = get_logger("http")

Handler = Callable[[Request], Awaitable[Response]]


def _route_template(request: Request) -> str:
    """The route pattern, not the concrete path.

    Logging the raw path would make access logs impossible to aggregate, since
    every submission UUID reads as a distinct endpoint.
    """
    route = request.scope.get("route")
    return getattr(route, "path", None) or "unmatched"


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        # Honour an upstream id so a trace spans the whole edge->API path.
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming.strip()[:64] or uuid.uuid4().hex
        request_id_ctx.set(request_id)
        user_id_ctx.set(None)
        session_id_ctx.set(None)
        request.state.request_id = request_id

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration = time.perf_counter() - started
            log.exception(
                "http.unhandled",
                method=request.method,
                path=request.url.path,
                duration_ms=round(duration * 1000, 2),
            )
            raise

        duration = time.perf_counter() - started
        route = _route_template(request)

        response.headers["x-request-id"] = request_id
        log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            route=route,
            status=response.status_code,
            duration_ms=round(duration * 1000, 2),
        )
        return response
