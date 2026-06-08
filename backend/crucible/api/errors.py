"""One error envelope for every failure mode.

Clients should never have to branch on which layer produced a failure, so
FastAPI validation errors, our HTTPExceptions and unhandled exceptions all
serialise to the same shape.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from crucible.core.config import settings
from crucible.core.logging import get_logger, request_id_ctx

log = get_logger("errors")


def _envelope(code: str, message: str, field: str | None = None) -> dict[str, object]:
    return {
        "error": {"code": code, "message": message, "field": field},
        "request_id": request_id_ctx.get(),
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            body = _envelope(
                str(detail.get("code")),
                str(detail.get("message", "")),
                detail.get("field"),  # type: ignore[arg-type]
            )
        else:
            body = _envelope(f"http_{exc.status_code}", str(detail))
        return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        # loc is ("body", "field", ...) -- drop the source segment.
        loc = [str(p) for p in first.get("loc", []) if p not in ("body", "query", "path")]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_envelope(
                "validation_error",
                str(first.get("msg", "Invalid request")),
                ".".join(loc) or None,
            ),
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error(_: Request, exc: IntegrityError) -> JSONResponse:
        # A constraint violation that reached here is a race we did not guard.
        # Log the detail; return a generic message, because constraint text
        # leaks schema internals to the caller.
        log.warning("db.integrity_error", error=str(exc.orig)[:300])
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_envelope("conflict", "That operation conflicts with existing data"),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("http.unhandled_exception", error=str(exc))
        # Never surface an internal message in production: stack traces and
        # driver errors are reconnaissance.
        message = str(exc) if settings.debug else "An unexpected error occurred"
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope("internal_error", message),
        )
