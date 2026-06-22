"""WebSocket endpoint for interview rooms.

Auth is the awkward part: browsers cannot set an Authorization header on a
WebSocket, so the access token arrives as a query parameter. That is a real
downside (query strings land in proxy and server logs), which is why the token
is short-lived and why the alternative would be a single-use ticket endpoint.
Documented rather than hidden -- see the note on `token` below.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from crucible.core import security
from crucible.core.logging import get_logger, request_id_ctx, user_id_ctx
from crucible.db.enums import RoomStatus
from crucible.db.models import InterviewRoom, User
from crucible.db.session import async_session_scope
from crucible.realtime.hub import Connection, hub
from crucible.realtime.protocol import (
    ClientMessage,
    ClientMessageType,
    Participant,
    ServerMessage,
    ServerMessageType,
)
from crucible.services import rooms as service

log = get_logger(__name__)
router = APIRouter()

# Close codes. 4401/4404 are in the private range, so a client can tell an auth
# failure from a normal disconnect without parsing a reason string.
CLOSE_UNAUTHORISED = 4401
CLOSE_NOT_FOUND = 4404
CLOSE_ENDED = 4410


async def _authenticate(token: str) -> User | None:
    try:
        claims = security.decode_token(token, expect="access")
    except security.AuthError:
        return None
    async with async_session_scope() as db:
        user = await db.get(User, claims.subject)
        if user is None or not user.is_active:
            return None
        db.expunge(user)
        return user


def _presence(room_id: uuid.UUID) -> list[Participant]:
    return [
        Participant(
            user_id=str(c.user_id),
            display_name=c.display_name,
            role=c.role,
            cursor=c.cursor,
        )
        for c in hub.participants(room_id)
    ]


@router.websocket("/ws/rooms/{room_id}")
async def room_socket(
    websocket: WebSocket,
    room_id: uuid.UUID,
    token: str = Query(
        ...,
        description=(
            "Access token. Sent as a query parameter because the browser "
            "WebSocket API cannot set request headers."
        ),
    ),
) -> None:
    user = await _authenticate(token)
    if user is None:
        await websocket.close(code=CLOSE_UNAUTHORISED)
        return

    # Membership is checked before accepting, so a non-participant never gets
    # an open socket to a room they are not in.
    async with async_session_scope() as db:
        room = (
            await db.execute(
                select(InterviewRoom)
                .where(InterviewRoom.id == room_id)
                .options(selectinload(InterviewRoom.participants))
            )
        ).scalar_one_or_none()

        if room is None:
            await websocket.close(code=CLOSE_NOT_FOUND)
            return
        if room.status is RoomStatus.ENDED:
            await websocket.close(code=CLOSE_ENDED)
            return

        participant = next((p for p in room.participants if p.user_id == user.id), None)
        if participant is None:
            await websocket.close(code=CLOSE_NOT_FOUND)
            return

        role = participant.role.value
        document = room.document
        version = room.document_version
        language = room.language

    await websocket.accept()
    connection = Connection(websocket, user.id, user.display_name, role)
    await hub.join(room_id, connection)
    user_id_ctx.set(str(user.id))
    request_id_ctx.set(connection.id.hex)

    try:
        # The joiner gets the whole document rather than replaying the log.
        await hub.send(
            connection,
            ServerMessage(
                type=ServerMessageType.SNAPSHOT,
                document=document,
                version=version,
                language=language,
                participants=_presence(room_id),
            ).dump(),
        )
        await hub.broadcast(
            room_id,
            ServerMessage(type=ServerMessageType.PRESENCE, participants=_presence(room_id)).dump(),
        )
        log.info("room.socket_open", room_id=str(room_id), role=role)

        while True:
            raw = await websocket.receive_text()
            await _handle(raw, room_id, connection)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("room.socket_error", room_id=str(room_id), error=str(exc))
    finally:
        await hub.leave(room_id, connection)
        await hub.broadcast(
            room_id,
            ServerMessage(type=ServerMessageType.PRESENCE, participants=_presence(room_id)).dump(),
        )
        log.info("room.socket_closed", room_id=str(room_id))


async def _handle(raw: str, room_id: uuid.UUID, connection: Connection) -> None:
    try:
        message = ClientMessage.model_validate_json(raw)
    except Exception:
        # A malformed frame is the client's problem, not grounds for closing
        # the socket and losing everyone's session.
        await hub.send(
            connection,
            ServerMessage(type=ServerMessageType.ERROR, message="Malformed message").dump(),
        )
        return

    if message.type is ClientMessageType.PING:
        await hub.send(connection, ServerMessage(type=ServerMessageType.PONG).dump())
        return

    if message.type is ClientMessageType.CURSOR:
        connection.cursor = message.cursor or 0
        await hub.broadcast(
            room_id,
            ServerMessage(
                type=ServerMessageType.CURSOR,
                actor=str(connection.user_id),
                cursor=connection.cursor,
            ).dump(),
            exclude=connection,
        )
        return

    if message.type is ClientMessageType.EDIT:
        if message.op is None or message.version is None:
            await hub.send(
                connection,
                ServerMessage(
                    type=ServerMessageType.ERROR, message="Edit needs an op and a version"
                ).dump(),
            )
            return

        async with async_session_scope() as db:
            room = await db.get(InterviewRoom, room_id)
            if room is None or room.status is RoomStatus.ENDED:
                return
            accepted, current, missed = await service.apply_edit(
                db, room, connection.user_id, message.version, message.op
            )

        if not accepted:
            # Send back what they missed so they can rebase, rather than
            # forcing a reload and losing their cursor.
            await hub.send(
                connection,
                ServerMessage(type=ServerMessageType.REBASE, version=current, ops=missed).dump(),
            )
            return

        await hub.broadcast(
            room_id,
            ServerMessage(
                type=ServerMessageType.EDIT,
                version=current,
                op=message.op,
                actor=str(connection.user_id),
            ).dump(),
            exclude=connection,
        )
        await hub.send(
            connection,
            ServerMessage(type=ServerMessageType.EDIT, version=current).dump(),
        )
        return

    if message.type is ClientMessageType.CHAT:
        text = (message.text or "").strip()
        if not text:
            return
        async with async_session_scope() as db:
            room = await db.get(InterviewRoom, room_id)
            if room is not None:
                await service.record_event(
                    db, room, connection.user_id, "chat", {"text": text[:4000]}
                )
        await hub.broadcast(
            room_id,
            ServerMessage(
                type=ServerMessageType.CHAT,
                text=text[:4000],
                actor=str(connection.user_id),
                actor_name=connection.display_name,
            ).dump(),
        )
        return

    if message.type is ClientMessageType.LANGUAGE:
        language = (message.language or "").strip().lower()
        if not language:
            return
        async with async_session_scope() as db:
            room = await db.get(InterviewRoom, room_id)
            if room is not None:
                room.language = language
                await service.record_event(
                    db, room, connection.user_id, "language", {"language": language}
                )
        await hub.broadcast(
            room_id,
            ServerMessage(
                type=ServerMessageType.LANGUAGE,
                language=language,
                actor=str(connection.user_id),
            ).dump(),
        )
        return

    if message.type is ClientMessageType.RUN:
        await _run_code(room_id, connection, message)


async def _run_code(room_id: uuid.UUID, connection: Connection, message: ClientMessage) -> None:
    """Run the room's current document in the sandbox.

    Goes through the same sandbox as a submission, on a worker thread so the
    event loop is not blocked for the seconds an execution takes. Everyone in
    the room sees the result, because watching someone debug is most of what an
    interview is.
    """
    import anyio

    from crucible.evaluation.sandbox import ExecutionRequest, get_sandbox

    async with async_session_scope() as db:
        room = await db.get(InterviewRoom, room_id)
        if room is None:
            return
        source, language = room.document, room.language
        await service.record_event(db, room, connection.user_id, "run", {"language": language})

    def _execute():
        return get_sandbox().execute(
            ExecutionRequest(language=language, source=source, stdin=message.text or "")
        )

    try:
        result = await anyio.to_thread.run_sync(_execute)
        payload = {
            "outcome": result.outcome.value,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_ms": result.duration_ms,
        }
    except Exception as exc:
        payload = {"outcome": "internal_error", "stdout": "", "stderr": str(exc)[:500]}

    await hub.broadcast(
        room_id,
        ServerMessage(
            type=ServerMessageType.RUN_RESULT,
            payload=payload,
            actor=str(connection.user_id),
            actor_name=connection.display_name,
        ).dump(),
    )
