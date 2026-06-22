"""WebSocket fan-out.

A dict of the sockets attached to this process; delivering to them is a direct
await. The durable state is the operation log in Postgres, so a process restart
drops in-flight fan-out but cannot corrupt a document: any client that
reconnects replays from the database and is correct again.

**This is single-process fan-out.** Two API processes do not see each other's
room traffic, so rooms require sticky routing to one process, or one process
full stop. That is the deliberate cost of dropping the Redis broker. Restoring
multi-process rooms does not need Redis back -- Postgres LISTEN/NOTIFY on a
per-room channel would carry the same envelopes -- it just needs writing.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import defaultdict

from fastapi import WebSocket

from crucible.core.logging import get_logger

log = get_logger(__name__)


class Connection:
    """One websocket, plus who is on the other end."""

    __slots__ = ("cursor", "display_name", "id", "role", "user_id", "websocket")

    def __init__(
        self, websocket: WebSocket, user_id: uuid.UUID, display_name: str, role: str
    ) -> None:
        self.websocket = websocket
        self.user_id = user_id
        self.display_name = display_name
        self.role = role
        self.cursor = 0
        # Distinguishes two tabs belonging to the same person.
        self.id = uuid.uuid4()


class RoomHub:
    """Process-local registry of connected sockets."""

    def __init__(self) -> None:
        self._rooms: dict[uuid.UUID, set[Connection]] = defaultdict(set)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle --

    async def start(self) -> None:
        log.info("rooms.hub_started")

    async def stop(self) -> None:
        async with self._lock:
            self._rooms.clear()

    # ------------------------------------------------------------- registry --

    async def join(self, room_id: uuid.UUID, connection: Connection) -> None:
        async with self._lock:
            self._rooms[room_id].add(connection)

    async def leave(self, room_id: uuid.UUID, connection: Connection) -> None:
        async with self._lock:
            self._rooms[room_id].discard(connection)
            if not self._rooms[room_id]:
                del self._rooms[room_id]

    def participants(self, room_id: uuid.UUID) -> list[Connection]:
        return list(self._rooms.get(room_id, ()))

    # ------------------------------------------------------------ broadcast --

    async def broadcast(
        self, room_id: uuid.UUID, payload: str, *, exclude: Connection | None = None
    ) -> None:
        """Deliver to every socket in this room attached to this process."""
        await self._send_local(room_id, payload, exclude=exclude)

    async def _send_local(
        self, room_id: uuid.UUID, payload: str, *, exclude: Connection | None
    ) -> None:
        dead: list[Connection] = []
        for connection in self.participants(room_id):
            if connection is exclude:
                continue
            try:
                await connection.websocket.send_text(payload)
            except Exception:
                # A socket that fails a send is gone. Collect and remove after
                # iterating, rather than mutating the set mid-loop.
                dead.append(connection)

        for connection in dead:
            await self.leave(room_id, connection)

    async def send(self, connection: Connection, payload: str) -> None:
        with contextlib.suppress(Exception):
            await connection.websocket.send_text(payload)


hub = RoomHub()
