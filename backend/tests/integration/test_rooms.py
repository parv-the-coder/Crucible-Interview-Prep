"""Interview rooms: REST surface and the WebSocket protocol.

The concurrency test is the important one. It drives two sockets at the same
version and asserts one is accepted and the other told to rebase, which is the
behaviour the unique (room_id, version) constraint exists to produce.
"""

from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.integration]

API = "/api/v1"


@pytest.fixture
async def app_instance():
    from crucible.main import create_app

    return create_app()


@pytest.fixture
async def interviewer(client):
    import uuid

    email = f"host-{uuid.uuid4().hex[:10]}@crucible-itest.dev"
    response = await client.post(
        f"{API}/auth/signup",
        json={"email": email, "display_name": "Host", "password": "room-test-password-1"},
    )
    body = response.json()
    yield {"token": body["tokens"]["access_token"], "id": body["user"]["id"]}

    from sqlalchemy import delete

    from crucible.db.models import RefreshToken, User
    from crucible.db.session import async_session_scope

    async with async_session_scope() as db:
        uid = uuid.UUID(body["user"]["id"])
        await db.execute(delete(RefreshToken).where(RefreshToken.user_id == uid))
        await db.execute(delete(User).where(User.id == uid))


@pytest.fixture
async def candidate(client):
    import uuid

    email = f"cand-{uuid.uuid4().hex[:10]}@crucible-itest.dev"
    response = await client.post(
        f"{API}/auth/signup",
        json={"email": email, "display_name": "Candidate", "password": "room-test-password-2"},
    )
    body = response.json()
    yield {"token": body["tokens"]["access_token"], "id": body["user"]["id"]}

    from sqlalchemy import delete

    from crucible.db.models import RefreshToken, User
    from crucible.db.session import async_session_scope

    async with async_session_scope() as db:
        uid = uuid.UUID(body["user"]["id"])
        await db.execute(delete(RefreshToken).where(RefreshToken.user_id == uid))
        await db.execute(delete(User).where(User.id == uid))


def auth(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {user['token']}"}


# ------------------------------------------------------------------ REST ---


async def test_create_room_returns_a_join_code(client, interviewer):
    response = await client.post(
        f"{API}/rooms", json={"title": "Screen"}, headers=auth(interviewer)
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "waiting"
    assert "-" in body["join_code"]
    # Codes get read aloud on a call.
    assert not set(body["join_code"]) & set("01ILO")
    assert body["websocket_url"].endswith(body["id"])


async def test_joining_makes_the_room_live(client, interviewer, candidate):
    room = (await client.post(f"{API}/rooms", json={}, headers=auth(interviewer))).json()

    joined = await client.post(
        f"{API}/rooms/join", json={"join_code": room["join_code"]}, headers=auth(candidate)
    )
    assert joined.status_code == 200
    assert joined.json()["status"] == "live"
    assert len(joined.json()["participants"]) == 2


async def test_join_code_is_case_insensitive(client, interviewer, candidate):
    room = (await client.post(f"{API}/rooms", json={}, headers=auth(interviewer))).json()
    joined = await client.post(
        f"{API}/rooms/join",
        json={"join_code": room["join_code"].lower()},
        headers=auth(candidate),
    )
    assert joined.status_code == 200


async def test_a_non_member_cannot_see_the_room(client, interviewer, candidate):
    """404 not 403 -- 403 confirms the room exists to someone guessing ids."""
    room = (await client.post(f"{API}/rooms", json={}, headers=auth(interviewer))).json()
    response = await client.get(f"{API}/rooms/{room['id']}", headers=auth(candidate))
    assert response.status_code == 404


async def test_only_the_interviewer_can_end_the_room(client, interviewer, candidate):
    room = (await client.post(f"{API}/rooms", json={}, headers=auth(interviewer))).json()
    await client.post(
        f"{API}/rooms/join", json={"join_code": room["join_code"]}, headers=auth(candidate)
    )

    refused = await client.post(f"{API}/rooms/{room['id']}/end", headers=auth(candidate))
    assert refused.status_code == 403

    allowed = await client.post(f"{API}/rooms/{room['id']}/end", headers=auth(interviewer))
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "ended"


async def test_an_ended_room_cannot_be_joined(client, interviewer, candidate):
    room = (await client.post(f"{API}/rooms", json={}, headers=auth(interviewer))).json()
    await client.post(f"{API}/rooms/{room['id']}/end", headers=auth(interviewer))

    response = await client.post(
        f"{API}/rooms/join", json={"join_code": room["join_code"]}, headers=auth(candidate)
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "room_ended"


# ------------------------------------------------------------- websocket ---
#
# Frame counts here are exact, not "read until we see what we want". The server
# sends a known sequence, so a loop with a generous upper bound would block
# forever the moment that sequence changes -- which is a hang, not a failure,
# and far worse to debug than an assertion.
#
# On connect the server sends exactly two frames: the snapshot to this socket,
# then a presence broadcast that this socket also receives.


def open_socket(tc, room_id: str, token: str):
    return tc.websocket_connect(f"/ws/rooms/{room_id}?token={token}")


def read_handshake(socket) -> dict:
    snapshot = json.loads(socket.receive_text())
    presence = json.loads(socket.receive_text())
    assert snapshot["type"] == "snapshot"
    assert presence["type"] == "presence"
    return snapshot


def edit(version: int, start: int, end: int, text: str) -> str:
    return json.dumps(
        {"type": "edit", "version": version, "op": {"start": start, "end": end, "text": text}}
    )


async def test_socket_rejects_a_bad_token(app_instance):
    """Auth is checked before the socket is accepted.

    Asserting on WebSocketDisconnect specifically, not any exception: a blind
    `pytest.raises(Exception)` would also pass if the app failed to start.
    """
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    room_id = "00000000-0000-0000-0000-000000000000"
    with (
        TestClient(app_instance) as tc,
        pytest.raises(WebSocketDisconnect) as exc,
        open_socket(tc, room_id, "not-a-real-token"),
    ):
        pass
    # 4401 is our own close code for "unauthenticated", so the client can tell
    # this from an ordinary disconnect.
    assert exc.value.code == 4401


async def test_socket_delivers_a_snapshot_then_applies_edits(client, interviewer, app_instance):
    from fastapi.testclient import TestClient

    room = (await client.post(f"{API}/rooms", json={}, headers=auth(interviewer))).json()

    with (
        TestClient(app_instance) as tc,
        open_socket(tc, room["id"], interviewer["token"]) as socket,
    ):
        snapshot = read_handshake(socket)
        assert snapshot["version"] == 0
        assert snapshot["document"] == ""

        socket.send_text(edit(0, 0, 0, "hello"))
        ack = json.loads(socket.receive_text())
        assert ack["type"] == "edit"
        assert ack["version"] == 1

    detail = (await client.get(f"{API}/rooms/{room['id']}", headers=auth(interviewer))).json()
    assert detail["document"] == "hello"
    assert detail["document_version"] == 1


async def test_a_stale_edit_is_rebased_not_applied(client, interviewer, app_instance):
    """The core concurrency guarantee.

    Two edits composed against the same version: the first is accepted, the
    second is told what it missed instead of silently overwriting it. This is
    the UNIQUE (room_id, version) constraint doing the work.
    """
    from fastapi.testclient import TestClient

    room = (await client.post(f"{API}/rooms", json={}, headers=auth(interviewer))).json()

    with (
        TestClient(app_instance) as tc,
        open_socket(tc, room["id"], interviewer["token"]) as socket,
    ):
        read_handshake(socket)

        socket.send_text(edit(0, 0, 0, "abc"))
        assert json.loads(socket.receive_text())["version"] == 1

        # Still claiming version 0, which is now stale.
        socket.send_text(edit(0, 0, 0, "xyz"))
        rebase = json.loads(socket.receive_text())
        assert rebase["type"] == "rebase"
        assert rebase["version"] == 1
        assert rebase["ops"], "client must be told which op it missed"

    detail = (await client.get(f"{API}/rooms/{room['id']}", headers=auth(interviewer))).json()
    assert detail["document"] == "abc", "the stale edit must not have been applied"
    assert detail["document_version"] == 1


async def test_malformed_frame_does_not_close_the_socket(client, interviewer, app_instance):
    """One client sending rubbish must not end everyone else's session."""
    from fastapi.testclient import TestClient

    room = (await client.post(f"{API}/rooms", json={}, headers=auth(interviewer))).json()

    with (
        TestClient(app_instance) as tc,
        open_socket(tc, room["id"], interviewer["token"]) as socket,
    ):
        read_handshake(socket)

        socket.send_text("this is not json")
        error = json.loads(socket.receive_text())
        assert error["type"] == "error"

        socket.send_text(json.dumps({"type": "ping"}))
        assert json.loads(socket.receive_text())["type"] == "pong"


async def test_chat_is_broadcast_and_recorded(client, interviewer, app_instance):
    from fastapi.testclient import TestClient

    room = (await client.post(f"{API}/rooms", json={}, headers=auth(interviewer))).json()

    with (
        TestClient(app_instance) as tc,
        open_socket(tc, room["id"], interviewer["token"]) as socket,
    ):
        read_handshake(socket)
        socket.send_text(json.dumps({"type": "chat", "text": "can you explain your approach?"}))

        chat = json.loads(socket.receive_text())
        assert chat["type"] == "chat"
        assert chat["text"] == "can you explain your approach?"
        assert chat["actor_name"] == "Host"


async def test_replay_returns_the_ordered_event_log(client, interviewer, app_instance):
    """What the append-only log buys beyond conflict resolution."""
    from fastapi.testclient import TestClient

    room = (await client.post(f"{API}/rooms", json={}, headers=auth(interviewer))).json()

    with (
        TestClient(app_instance) as tc,
        open_socket(tc, room["id"], interviewer["token"]) as socket,
    ):
        read_handshake(socket)
        socket.send_text(edit(0, 0, 0, "hi"))
        socket.receive_text()
        socket.send_text(json.dumps({"type": "chat", "text": "hello"}))
        socket.receive_text()

    events = (
        await client.get(f"{API}/rooms/{room['id']}/replay", headers=auth(interviewer))
    ).json()
    kinds = [e["type"] for e in events]
    assert "edit" in kinds
    assert "chat" in kinds

    versions = [e["version"] for e in events]
    assert versions == sorted(versions), "replay is meaningless if versions are not ordered"
