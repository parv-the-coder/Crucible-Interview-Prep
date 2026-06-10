"""Integration test fixtures.

These run against the real Postgres from infra/docker-compose.yml:

    docker compose -f infra/docker-compose.yml up -d
    pytest -m integration

Everything runs inside a transaction that is rolled back after each test, so
the suite leaves the database exactly as it found it -- except where a test
deliberately verifies commit behaviour, which is the point of
test_auth_flow.py.
"""

from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")
os.environ.setdefault("SANDBOX_BACKEND", "subprocess")

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, text


@pytest.fixture(scope="session", autouse=True)
async def require_database():
    """Skip cleanly when the stack is not running.

    Async on purpose. Creating a loop by hand here would build the cached
    asyncpg engine inside a loop that is then thrown away, and every later
    test would fail with "attached to a different loop".
    """
    from crucible.db.session import get_async_engine

    try:
        async with get_async_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"Postgres is not reachable -- start infra/docker-compose.yml ({exc})")

    yield
    await get_async_engine().dispose()


@pytest.fixture
async def client():
    from crucible.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def registered_user(client):
    """A freshly registered account, unique per test."""
    email = f"itest-{uuid.uuid4().hex[:12]}@crucible-itest.dev"
    password = "integration-test-password-9"
    response = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "display_name": "Integration Test", "password": password},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    yield {
        "email": email,
        "password": password,
        "id": body["user"]["id"],
        "access_token": body["tokens"]["access_token"],
        "refresh_token": body["tokens"]["refresh_token"],
    }

    # Clean up. This user really was committed, and deleting it cascades to
    # their sessions and submissions.
    #
    # Retried on deadlock because a queue worker may be grading this user's
    # submissions at the same moment. The worker locks a submission and then
    # touches its session; this delete locks the user and cascades outward, so
    # the two can meet in opposite order. Postgres kills one of them, and the
    # loser here is a test fixture with nothing to lose by trying again.
    import asyncio

    from sqlalchemy.exc import DBAPIError

    from crucible.db.models import RefreshToken, User
    from crucible.db.session import async_session_scope

    user_id = uuid.UUID(body["user"]["id"])
    for attempt in range(3):
        try:
            async with async_session_scope() as db:
                await db.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
            break
        except DBAPIError:
            if attempt == 2:
                raise
            await asyncio.sleep(0.5 * (attempt + 1))
