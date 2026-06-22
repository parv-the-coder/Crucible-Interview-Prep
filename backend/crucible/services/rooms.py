"""Live interview rooms.

Concurrency control is a unique constraint. `room_events` has
`UNIQUE (room_id, version)`, so two clients racing to write version N means one
INSERT fails; that client is sent the operation it missed, rebases and retries.

Postgres does the hard part. See docs/adr/0009-operation-log-for-rooms.md for
why this rather than a CRDT.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from crucible.core.config import settings
from crucible.core.logging import get_logger
from crucible.core.security import generate_join_code
from crucible.db.enums import RoomRole, RoomStatus, UserRole
from crucible.db.models import InterviewRoom, Question, RoomEvent, RoomParticipant, User
from crucible.realtime.protocol import EditOp
from crucible.schemas.common import Page
from crucible.schemas.room import RoomCreate, RoomDetail, RoomSummary

log = get_logger(__name__)

# Rebuild the materialised document every this many ops, so a late joiner
# replays a short tail instead of the whole session.
SNAPSHOT_EVERY = 50


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "room_not_found", "message": "No such room"},
    )


async def create_room(db: AsyncSession, host: User, payload: RoomCreate) -> RoomDetail:
    if payload.question_id is not None:
        question = await db.get(Question, payload.question_id)
        if question is None or not question.is_active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "question_not_found", "message": "No such question"},
            )

    # Retry on collision rather than trusting 34 billion to be enough. The
    # unique index is the real guarantee; this just avoids surfacing it.
    for _ in range(5):
        room = InterviewRoom(
            join_code=generate_join_code(),
            title=payload.title or "Interview",
            host_id=host.id,
            question_id=payload.question_id,
            language=payload.language,
            is_recorded=payload.is_recorded,
            last_activity_at=datetime.now(UTC),
        )
        db.add(room)
        try:
            await db.flush()
            break
        except IntegrityError:
            await db.rollback()
    else:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "join_code_exhausted", "message": "Could not allocate a room code"},
        )

    db.add(
        RoomParticipant(
            room_id=room.id,
            user_id=host.id,
            role=RoomRole.INTERVIEWER,
            joined_at=datetime.now(UTC),
        )
    )
    await db.flush()
    log.info("room.created", room_id=str(room.id), join_code=room.join_code)
    return await _to_detail(db, room)


async def _to_detail(db: AsyncSession, room: InterviewRoom) -> RoomDetail:
    await db.refresh(room, ["participants"])
    detail = RoomDetail.model_validate(room)
    detail.websocket_url = f"/ws/rooms/{room.id}"
    return detail


async def get_room(db: AsyncSession, user: User, room_id: uuid.UUID) -> RoomDetail:
    room = await _load(db, room_id)
    await _assert_member(db, room, user)
    return await _to_detail(db, room)


async def _load(db: AsyncSession, room_id: uuid.UUID) -> InterviewRoom:
    room = (
        await db.execute(
            select(InterviewRoom)
            .where(InterviewRoom.id == room_id)
            .options(selectinload(InterviewRoom.participants))
        )
    ).scalar_one_or_none()
    if room is None:
        raise _not_found()
    return room


async def _assert_member(db: AsyncSession, room: InterviewRoom, user: User) -> RoomParticipant:
    """Rooms are private to their participants.

    404 rather than 403 for a non-member: 403 confirms the room exists, and a
    join code is guessable enough that we should not help.
    """
    for participant in room.participants:
        if participant.user_id == user.id:
            return participant
    if user.role is UserRole.ADMIN:
        return RoomParticipant(room_id=room.id, user_id=user.id, role=RoomRole.OBSERVER)
    raise _not_found()


async def join_room(db: AsyncSession, user: User, join_code: str) -> RoomDetail:
    code = join_code.strip().upper()
    room = (
        await db.execute(
            select(InterviewRoom)
            .where(InterviewRoom.join_code == code)
            .options(selectinload(InterviewRoom.participants))
        )
    ).scalar_one_or_none()

    if room is None:
        raise _not_found()
    if room.status is RoomStatus.ENDED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "room_ended", "message": "This interview has ended"},
        )

    existing = next((p for p in room.participants if p.user_id == user.id), None)
    if existing is None:
        active = [p for p in room.participants if p.left_at is None]
        if len(active) >= settings.room_max_participants:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "room_full", "message": "This room is full"},
            )
        # First person in is the interviewer; everyone after is a candidate.
        role = RoomRole.CANDIDATE if room.participants else RoomRole.INTERVIEWER
        db.add(
            RoomParticipant(
                room_id=room.id, user_id=user.id, role=role, joined_at=datetime.now(UTC)
            )
        )
    else:
        existing.left_at = None
        existing.connection_epoch += 1

    if room.status is RoomStatus.WAITING:
        room.status = RoomStatus.LIVE
        room.started_at = datetime.now(UTC)
    room.last_activity_at = datetime.now(UTC)

    await db.flush()
    log.info("room.joined", room_id=str(room.id), user_id=str(user.id))
    return await _to_detail(db, room)


async def apply_edit(
    db: AsyncSession, room: InterviewRoom, actor_id: uuid.UUID, version: int, op: EditOp
) -> tuple[bool, int, list[dict]]:
    """Try to append an edit at `version`.

    Returns `(accepted, current_version, missed_ops)`. When the version is
    stale the caller sends `missed_ops` back so the client can rebase rather
    than reload.
    """
    if version != room.document_version:
        missed = (
            (
                await db.execute(
                    select(RoomEvent)
                    .where(RoomEvent.room_id == room.id, RoomEvent.version > version)
                    .order_by(RoomEvent.version)
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        return (
            False,
            room.document_version,
            [
                {"version": e.version, "op": e.payload, "actor": str(e.actor_id or "")}
                for e in missed
            ],
        )

    next_version = version + 1
    db.add(
        RoomEvent(
            room_id=room.id,
            version=next_version,
            actor_id=actor_id,
            event_type="edit",
            payload=op.model_dump(),
        )
    )

    try:
        await db.flush()
    except IntegrityError:
        # Someone else took this version between the check and the insert.
        # The unique constraint is what actually serialises writers; the
        # comparison above is only a fast path.
        await db.rollback()
        return False, room.document_version, []

    room.document = op.apply(room.document)
    room.document_version = next_version
    room.last_activity_at = datetime.now(UTC)

    if next_version - room.snapshot_at_version >= SNAPSHOT_EVERY:
        room.snapshot_at_version = next_version

    return True, next_version, []


async def record_event(
    db: AsyncSession,
    room: InterviewRoom,
    actor_id: uuid.UUID | None,
    event_type: str,
    payload: dict,
) -> None:
    """Append a non-edit event (chat, language change, run).

    Recorded so a session can be replayed afterwards, which is the point of
    keeping a log rather than only the current document.
    """
    if not room.is_recorded:
        return
    room.document_version += 1
    db.add(
        RoomEvent(
            room_id=room.id,
            version=room.document_version,
            actor_id=actor_id,
            event_type=event_type,
            payload=payload,
        )
    )
    room.last_activity_at = datetime.now(UTC)


async def end_room(db: AsyncSession, user: User, room_id: uuid.UUID) -> RoomDetail:
    room = await _load(db, room_id)
    participant = await _assert_member(db, room, user)
    if participant.role is not RoomRole.INTERVIEWER and user.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "not_the_interviewer", "message": "Only the interviewer can end it"},
        )
    room.status = RoomStatus.ENDED
    room.ended_at = datetime.now(UTC)
    await db.flush()
    return await _to_detail(db, room)


async def save_feedback(
    db: AsyncSession, user: User, room_id: uuid.UUID, notes: str, feedback: dict
) -> RoomDetail:
    room = await _load(db, room_id)
    participant = await _assert_member(db, room, user)
    if participant.role is not RoomRole.INTERVIEWER and user.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "not_the_interviewer", "message": "Only the interviewer can do that"},
        )
    room.notes = notes
    room.feedback = feedback
    await db.flush()
    return await _to_detail(db, room)


async def list_rooms(
    db: AsyncSession, user: User, *, limit: int = 20, offset: int = 0
) -> Page[RoomSummary]:
    """Rooms this user is in, newest first."""
    mine = select(RoomParticipant.room_id).where(RoomParticipant.user_id == user.id)
    rows = (
        (
            await db.execute(
                select(InterviewRoom)
                .where(InterviewRoom.id.in_(mine))
                .order_by(InterviewRoom.created_at.desc())
                .limit(limit + 1)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return Page[RoomSummary](
        items=[RoomSummary.model_validate(r) for r in rows[:limit]],
        limit=limit,
        offset=offset,
        has_more=len(rows) > limit,
    )


async def replay(db: AsyncSession, user: User, room_id: uuid.UUID) -> list[dict]:
    """Every recorded event, in order. Used for session playback."""
    room = await _load(db, room_id)
    await _assert_member(db, room, user)
    events = (
        (
            await db.execute(
                select(RoomEvent).where(RoomEvent.room_id == room.id).order_by(RoomEvent.version)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "version": e.version,
            "type": e.event_type,
            "actor": str(e.actor_id) if e.actor_id else None,
            "payload": e.payload,
            "at": e.created_at.isoformat(),
        }
        for e in events
    ]
