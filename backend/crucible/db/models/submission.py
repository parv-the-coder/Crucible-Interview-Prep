from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from crucible.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from crucible.db.enums import (
    QuestionType,
    SubmissionStatus,
    TestCaseOutcome,
    pg_enum,
)

if TYPE_CHECKING:
    from crucible.db.models.user import User


class Submission(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One evaluated attempt at a question.

    A row in QUEUED *is* the queue entry -- there is no separate job record --
    so the INSERT that accepts a submission is also what schedules it. Rows only
    ever advance forward through the status machine:

        queued -> running -> completed | failed
                     `-> (worker crash) -> requeued by the reaper

    The status transition itself is done with a conditional UPDATE, which is
    what makes a duplicate claim harmless -- see docs/07.
    """

    __tablename__ = "submissions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("questions.id", ondelete="RESTRICT"), nullable=False
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_sessions.id", ondelete="SET NULL")
    )
    room_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("interview_rooms.id", ondelete="SET NULL")
    )

    # Client-supplied key. Unique per user, so a retried POST after a network
    # timeout returns the original submission instead of double-charging the
    # sandbox. Sparse: only enforced when the client actually sends one.
    idempotency_key: Mapped[str | None] = mapped_column(String(64))

    type: Mapped[QuestionType] = mapped_column(
        pg_enum(QuestionType, "question_type"), nullable=False
    )
    language: Mapped[str | None] = mapped_column(String(20))
    source_code: Mapped[str] = mapped_column(Text, default="", nullable=False)
    answer: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False, server_default="{}"
    )
    # A "run" executes sample cases only and is not graded or rated.
    is_dry_run: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )

    status: Mapped[SubmissionStatus] = mapped_column(
        pg_enum(SubmissionStatus, "submission_status"),
        default=SubmissionStatus.QUEUED,
        nullable=False,
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(120))

    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queue_wait_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    execution_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    peak_memory_kb: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    max_score: Mapped[float] = mapped_column(Float, default=100.0, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cases_passed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cases_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    compile_output: Mapped[str] = mapped_column(Text, default="", nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # Populated asynchronously by the AI reviewer; null until that lands.
    ai_review_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    user: Mapped[User] = relationship(back_populates="submissions")
    results: Mapped[list[SubmissionResult]] = relationship(
        back_populates="submission",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SubmissionResult.ordinal",
    )

    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="user_idempotency"),
        CheckConstraint("score >= 0 AND score <= max_score", name="score_in_range"),
        CheckConstraint("cases_passed <= cases_total", name="cases_consistent"),
        CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        # Submission history: "my submissions, newest first".
        Index("ix_submissions_user_recent", "user_id", "created_at"),
        Index("ix_submissions_question_recent", "question_id", "created_at"),
        Index("ix_submissions_session", "session_id", "created_at"),
        # The stuck-job reaper scans only rows that are mid-flight. A partial
        # index keeps this scan O(stuck) rather than O(all submissions).
        Index(
            "ix_submissions_inflight",
            "started_at",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        # Leaderboard / analytics: graded passes per user per question.
        Index(
            "ix_submissions_graded",
            "user_id",
            "question_id",
            "created_at",
            postgresql_where=text("status = 'completed' AND NOT is_dry_run"),
        ),
    )


class SubmissionResult(Base, UUIDPrimaryKeyMixin):
    """Per-test-case outcome.

    Deliberately not a JSON blob on Submission: keeping it relational lets us
    ask "which test case has the highest failure rate across all users", which
    is how you find a badly-specified question.
    """

    __tablename__ = "submission_results"

    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False
    )
    test_case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_cases.id", ondelete="SET NULL")
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    outcome: Mapped[TestCaseOutcome] = mapped_column(
        pg_enum(TestCaseOutcome, "test_case_outcome"), nullable=False
    )
    execution_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    memory_kb: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)

    # Only ever persisted for sample cases; hidden-case output would leak the
    # answer key through the API.
    stdout: Mapped[str] = mapped_column(Text, default="", nullable=False)
    stderr: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_visible: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )

    submission: Mapped[Submission] = relationship(back_populates="results")

    __table_args__ = (
        UniqueConstraint("submission_id", "ordinal", name="submission_ordinal"),
        Index("ix_submission_results_case_outcome", "test_case_id", "outcome"),
    )
