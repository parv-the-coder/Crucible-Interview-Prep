from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from crucible.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from crucible.db.enums import (
    AIPurpose,
    pg_enum,
)


class AIInteraction(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Audit + cost ledger for every LLM call.

    Three reasons this table exists rather than just logging:
      1. Per-user daily budgets are enforced by counting rows here.
      2. A model's output that becomes user-visible feedback must be
         reproducible if a candidate disputes a grade.
      3. Prompt/response pairs are the dataset for evaluating a prompt change.
    """

    __tablename__ = "ai_interactions"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    submission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("submissions.id", ondelete="CASCADE")
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_sessions.id", ondelete="CASCADE")
    )
    room_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("interview_rooms.id", ondelete="CASCADE")
    )

    purpose: Mapped[AIPurpose] = mapped_column(pg_enum(AIPurpose, "ai_purpose"), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)

    # Hash of the rendered prompt -> lets us cache identical requests and
    # detect prompt drift between deploys without storing duplicates.
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False, server_default="{}"
    )

    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    ok: Mapped[bool] = mapped_column(default=True, nullable=False, server_default=text("true"))
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # True when served from the response cache rather than the provider.
    cached: Mapped[bool] = mapped_column(
        default=False, nullable=False, server_default=text("false")
    )

    __table_args__ = (
        CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0", name="tokens_non_negative"
        ),
        # Budget check: "how many billable calls has this user made today".
        Index(
            "ix_ai_interactions_budget",
            "user_id",
            "created_at",
            postgresql_where=text("NOT cached AND ok"),
        ),
        Index("ix_ai_interactions_submission", "submission_id"),
        Index("ix_ai_interactions_purpose_time", "purpose", "created_at"),
    )
