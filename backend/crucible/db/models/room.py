from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
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
    RoomRole,
    RoomStatus,
    pg_enum,
)


class InterviewRoom(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A live collaborative interview room.

    The room's shared editor is an append-only operation log (see RoomEvent).
    Postgres holds the durable log and the periodic snapshot; fan-out to live
    sockets is transport only. Nothing about correctness depends on it -- a
    client that misses a frame reconnects and replays the log.
    """

    __tablename__ = "interview_rooms"

    # Short human-shareable code, e.g. "PX7-K2M". Not the primary key: we never
    # want a guessable identifier to be the thing FKs point at.
    join_code: Mapped[str] = mapped_column(String(12), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    host_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("questions.id", ondelete="SET NULL")
    )

    status: Mapped[RoomStatus] = mapped_column(
        pg_enum(RoomStatus, "room_status"),
        default=RoomStatus.WAITING,
        nullable=False,
    )
    language: Mapped[str] = mapped_column(String(20), default="python", nullable=False)

    # Materialised editor state. Rebuilt from the event log on demand, but
    # snapshotted so a joiner does not have to replay thousands of ops.
    document: Mapped[str] = mapped_column(Text, default="", nullable=False)
    document_version: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False, server_default="0"
    )
    snapshot_at_version: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False, server_default="0"
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    is_recorded: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=text("true")
    )
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    feedback: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False, server_default="{}"
    )

    participants: Mapped[list[RoomParticipant]] = relationship(
        back_populates="room", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint("document_version >= snapshot_at_version", name="version_ordered"),
        # Idle-room reaper scans only rooms that are still open.
        Index(
            "ix_interview_rooms_open",
            "last_activity_at",
            postgresql_where=text("status <> 'ended'"),
        ),
        Index("ix_interview_rooms_host", "host_id", "created_at"),
    )


class RoomParticipant(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "room_participants"

    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("interview_rooms.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[RoomRole] = mapped_column(pg_enum(RoomRole, "room_role"), nullable=False)

    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Bumped on every (re)connect so a stale socket cannot resurrect presence.
    connection_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    room: Mapped[InterviewRoom] = relationship(back_populates="participants")

    __table_args__ = (
        UniqueConstraint("room_id", "user_id", name="room_user"),
        # At most one interviewer holds the "driver" role at a time; enforced
        # in the service layer, indexed here for the presence query.
        Index("ix_room_participants_room_role", "room_id", "role"),
    )


class RoomEvent(Base, TimestampMixin):
    """Append-only operation log for one room.

    `(room_id, version)` is unique, which is exactly the optimistic-concurrency
    check: two clients racing to write version N means one INSERT fails, gets
    the winner's op, rebases, and retries. That is the whole conflict
    resolution mechanism -- see docs/09.
    """

    __tablename__ = "room_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("interview_rooms.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False, server_default="{}"
    )

    __table_args__ = (
        UniqueConstraint("room_id", "version", name="room_version"),
        Index("ix_room_events_replay", "room_id", "version"),
    )
