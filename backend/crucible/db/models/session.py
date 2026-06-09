from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from crucible.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from crucible.db.enums import (
    Difficulty,
    SessionStatus,
    ViolationAction,
    ViolationKind,
    pg_enum,
)

if TYPE_CHECKING:
    from crucible.db.models.question import Question
    from crucible.db.models.user import User


class TestSession(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A timed, proctored practice test."""

    __tablename__ = "test_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    topics: Mapped[list[str]] = mapped_column(
        ARRAY(String(80)), default=list, nullable=False, server_default="{}"
    )
    question_types: Mapped[list[str]] = mapped_column(
        ARRAY(String(20)), default=list, nullable=False, server_default="{}"
    )
    target_difficulty: Mapped[Difficulty | None] = mapped_column(pg_enum(Difficulty, "difficulty"))
    is_adaptive: Mapped[bool] = mapped_column(
        default=False, nullable=False, server_default=text("false")
    )

    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Server-authoritative deadline. The client countdown is decoration; every
    # answer write is re-checked against this column.
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[SessionStatus] = mapped_column(
        pg_enum(SessionStatus, "session_status"),
        default=SessionStatus.ACTIVE,
        nullable=False,
    )
    violation_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    total_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    max_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    debrief: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False, server_default="{}"
    )

    user: Mapped[User] = relationship(back_populates="sessions")
    items: Mapped[list[SessionItem]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SessionItem.ordinal",
    )
    violations: Mapped[list[Violation]] = relationship(
        back_populates="session", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="window_ordered"),
        CheckConstraint("duration_seconds BETWEEN 60 AND 21600", name="duration_range"),
        CheckConstraint("violation_count >= 0", name="violations_non_negative"),
        Index("ix_test_sessions_user_recent", "user_id", "created_at"),
        # The expiry sweeper scans only live sessions past their deadline.
        Index(
            "ix_test_sessions_expiring",
            "ends_at",
            postgresql_where=text("status = 'active'"),
        ),
    )


class SessionItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One question slot inside a session, plus the candidate's working state.

    The in-progress answer lives here (not on Submission) so a browser crash
    loses nothing: the editor autosaves into this row, and only an explicit
    run/submit creates a Submission.
    """

    __tablename__ = "session_items"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_sessions.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("questions.id", ondelete="RESTRICT"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    draft_language: Mapped[str | None] = mapped_column(String(20))
    draft_code: Mapped[str] = mapped_column(Text, default="", nullable=False)
    draft_answer: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False, server_default="{}"
    )
    last_saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    final_submission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("submissions.id", ondelete="SET NULL")
    )
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    max_score: Mapped[float] = mapped_column(Float, default=100.0, nullable=False)
    time_spent_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    session: Mapped[TestSession] = relationship(back_populates="items")
    question: Mapped[Question] = relationship(lazy="joined")

    __table_args__ = (
        UniqueConstraint("session_id", "ordinal", name="session_ordinal"),
        # A question may not appear twice in the same session.
        UniqueConstraint("session_id", "question_id", name="session_question"),
        CheckConstraint("score >= 0 AND score <= max_score", name="score_in_range"),
    )


class Violation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Immutable proctoring audit trail.

    Append-only on purpose: if a result is ever disputed, the evidence must not
    be something the application can quietly rewrite.
    """

    __tablename__ = "violations"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_sessions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[ViolationKind] = mapped_column(
        pg_enum(ViolationKind, "violation_kind"), nullable=False
    )
    action: Mapped[ViolationAction] = mapped_column(
        pg_enum(ViolationAction, "violation_action"), nullable=False
    )
    running_count: Mapped[int] = mapped_column(Integer, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False, server_default="{}"
    )
    request_id: Mapped[str | None] = mapped_column(String(64))

    session: Mapped[TestSession] = relationship(back_populates="violations")

    __table_args__ = (Index("ix_violations_session_time", "session_id", "created_at"),)
