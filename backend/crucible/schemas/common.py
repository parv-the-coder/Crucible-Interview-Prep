"""Shared response shapes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    """Base for anything read out of SQLAlchemy."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class Page[T](BaseModel):
    """Keyset-friendly page envelope.

    ``total`` is optional on purpose: COUNT(*) over a large filtered table is
    the expensive half of a listing query, and most UIs only need to know
    whether a next page exists.
    """

    items: list[T]
    limit: int
    offset: int
    total: int | None = None
    has_more: bool = False


class ErrorDetail(BaseModel):
    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable explanation.")
    field: str | None = Field(default=None, description="Offending input field, if any.")


class Health(BaseModel):
    status: str
    version: str
    environment: str
    checks: dict[str, dict[str, object]]
