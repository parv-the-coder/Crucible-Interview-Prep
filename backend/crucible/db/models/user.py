from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from crucible.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from crucible.db.enums import (
    Difficulty,
    UserRole,
    pg_enum,
)

if TYPE_CHECKING:
    from crucible.db.models.session import TestSession
    from crucible.db.models.submission import Submission


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        pg_enum(UserRole, "user_role"),
        default=UserRole.STUDENT,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Global skill rating, updated by the adaptive engine after each graded
    # submission. Seeded at 1200 (standard Elo convention).
    rating: Mapped[float] = mapped_column(Float, default=1200.0, nullable=False)

    submissions: Mapped[list[Submission]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    sessions: Mapped[list[TestSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint("position('@' in email) > 1", name="email_shape"),
        CheckConstraint("rating >= 0", name="rating_non_negative"),
        # Admin dashboards list active users newest-first; the partial index
        # keeps deactivated accounts out of the hot path entirely.
        Index(
            "ix_users_active_recent",
            "created_at",
            postgresql_where=text("is_active"),
        ),
    )


class RefreshToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Refresh tokens with rotation + reuse detection.

    We store only a SHA-256 of the token. `family_id` links every token derived
    from one login; if a rotated token is ever replayed we revoke the whole
    family, which is the standard mitigation for refresh-token theft.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(String(45))

    __table_args__ = (Index("ix_refresh_tokens_user_active", "user_id", "expires_at"),)


class TopicMastery(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Per-user, per-topic skill rating.

    Kept as a row rather than recomputed from the submission log on every
    request: the adaptive selector reads it on the hot path, and aggregating
    a user's whole submission history to pick one question is wasteful.
    """

    __tablename__ = "topic_mastery"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    topic: Mapped[str] = mapped_column(String(80), nullable=False)

    rating: Mapped[float] = mapped_column(Float, default=1200.0, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    passes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_difficulty: Mapped[Difficulty | None] = mapped_column(pg_enum(Difficulty, "difficulty"))

    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("user_id", "topic", name="user_topic"),
        CheckConstraint("passes <= attempts", name="passes_le_attempts"),
        Index("ix_topic_mastery_user", "user_id", "rating"),
    )
