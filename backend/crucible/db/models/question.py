from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
    QuestionType,
    pg_enum,
)


class Question(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "questions"

    slug: Mapped[str] = mapped_column(String(140), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    constraints_md: Mapped[str] = mapped_column(Text, default="", nullable=False)

    type: Mapped[QuestionType] = mapped_column(
        pg_enum(QuestionType, "question_type"), nullable=False
    )
    difficulty: Mapped[Difficulty] = mapped_column(
        pg_enum(Difficulty, "difficulty"), nullable=False
    )
    topic: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), default=list, nullable=False, server_default="{}"
    )

    # Elo rating for the *question*, updated alongside user ratings. A question
    # nobody solves drifts upward and stops being handed to beginners.
    rating: Mapped[float] = mapped_column(Float, default=1200.0, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pass_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    time_limit_ms: Mapped[int] = mapped_column(Integer, default=5000, nullable=False)
    memory_limit_mb: Mapped[int] = mapped_column(Integer, default=256, nullable=False)
    allowed_languages: Mapped[list[str]] = mapped_column(
        ARRAY(String(20)), default=list, nullable=False, server_default="{}"
    )
    starter_code: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False, server_default="{}"
    )
    reference_solution: Mapped[str | None] = mapped_column(Text)

    # MCQ payload:  {"choices": [...], "correct": ["b"], "explanation": "..."}
    # SQL payload:  {"schema_sql": "...", "seed_sql": "...", "expected_rows": [...]}
    # Design/behavioural payload: {"rubric": [{"criterion":..., "weight":...}]}
    # JSONB because the shape is genuinely per-type; the relational part (test
    # cases) lives in its own table.
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False, server_default="{}"
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_ai_generated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    test_cases: Mapped[list[TestCase]] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TestCase.ordinal",
    )
    __table_args__ = (
        CheckConstraint("pass_count <= attempt_count", name="pass_le_attempt"),
        CheckConstraint("time_limit_ms BETWEEN 100 AND 60000", name="time_limit_range"),
        CheckConstraint("memory_limit_mb BETWEEN 16 AND 2048", name="memory_limit_range"),
        # The adaptive selector filters on active + type + difficulty and sorts
        # by rating; this index serves that query without a heap sort.
        Index(
            "ix_questions_selector",
            "type",
            "difficulty",
            "rating",
            postgresql_where=text("is_active"),
        ),
        Index(
            "ix_questions_topic_active",
            "topic",
            "difficulty",
            postgresql_where=text("is_active"),
        ),
        Index("ix_questions_tags", "tags", postgresql_using="gin"),
        # Full-text search for the question browser.
        Index(
            "ix_questions_fts",
            text("to_tsvector('english', title || ' ' || prompt)"),
            postgresql_using="gin",
        ),
    )


class TestCase(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One input/expected-output pair.

    Modelled relationally rather than as a JSON blob so per-case results can
    foreign-key to it -- that is what makes "which exact case regressed"
    answerable with a query instead of a script.
    """

    __tablename__ = "test_cases"

    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("questions.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    stdin: Mapped[str] = mapped_column(Text, default="", nullable=False)
    expected_stdout: Mapped[str] = mapped_column(Text, nullable=False)
    # Sample cases are shown to the candidate; hidden ones only affect scoring.
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, default="", nullable=False)

    question: Mapped[Question] = relationship(back_populates="test_cases")

    __table_args__ = (
        UniqueConstraint("question_id", "ordinal", name="question_ordinal"),
        CheckConstraint("weight > 0", name="weight_positive"),
        Index("ix_test_cases_sample", "question_id", postgresql_where=text("is_sample")),
    )
