"""Engine and session management.

Two engines exist on purpose:

* ``async_engine`` (asyncpg) serves the FastAPI request path, where almost all
  work is I/O-bound and concurrency comes from the event loop.
* ``sync_engine`` (psycopg) serves the queue worker and Alembic. The worker is
  a plain synchronous loop; wrapping every job in ``asyncio.run`` to reach the
  database buys nothing and makes shutdown and connection cleanup harder.

See docs/adr/0003-two-database-engines.md.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from crucible.core.config import settings
from crucible.core.logging import get_logger

log = get_logger(__name__)


@lru_cache
def get_async_engine() -> AsyncEngine:
    # No pool under test. An asyncpg pool is bound to the event loop that
    # created it, and the test suite legitimately runs more than one loop --
    # pytest-asyncio has its own, and Starlette's TestClient starts another to
    # drive WebSockets. A pooled connection created in one and reused in the
    # other fails with "attached to a different loop", which looks like a bug
    # in the code under test and is not. NullPool opens per use, so it is safe
    # across loops and costs nothing at test volumes.
    if settings.environment == "test":
        return create_async_engine(
            settings.async_database_url,
            echo=settings.db_echo,
            poolclass=NullPool,
            connect_args={"statement_cache_size": 0},
        )

    return create_async_engine(
        settings.async_database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        # Recycle below typical cloud idle-connection timeouts so we never hand
        # out a socket the far end already closed.
        pool_recycle=1800,
        pool_pre_ping=True,
        connect_args={
            # Server-side statement cache breaks with pgbouncer in txn mode.
            "statement_cache_size": 0,
            "server_settings": {"application_name": "crucible-api"},
        },
    )


@lru_cache
def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_async_engine(),
        class_=AsyncSession,
        expire_on_commit=False,  # keep ORM objects usable after commit
        autoflush=False,
    )


@lru_cache
def get_sync_engine():
    return create_engine(
        settings.sync_database_url,
        echo=settings.db_echo,
        # Each worker runs as its own process; a pool inherited
        # across fork() is a classic source of "connection already closed".
        poolclass=NullPool,
        connect_args={"application_name": "crucible-worker"},
    )


@lru_cache
def get_sync_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_sync_engine(), expire_on_commit=False, autoflush=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency.

    One session per request, committed on success and rolled back on any
    exception. Routes never commit -- the boundary owns the transaction, so a
    handler that raises halfway cannot leave a partial write behind.
    """
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


@asynccontextmanager
async def async_session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope for code outside the request path."""
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


@contextmanager
def sync_session_scope() -> Iterator[Session]:
    """Transactional scope for the worker and other sync callers."""
    factory = get_sync_session_factory()
    session = factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    else:
        session.commit()
    finally:
        session.close()
