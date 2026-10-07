# ADR-0003: Separate async and sync SQLAlchemy engines

Status: Accepted
Date: 2026-08-23

## Context

FastAPI is asynchronous. The queue worker is a synchronous loop. Both
need database access, and SQLAlchemy 2.0 supports either style, but a single
engine cannot serve both.

## Options considered

### Option A: One async engine; workers wrap every task in `asyncio.run()`
**Pros.** One engine, one session factory, one mental model.
**Cons.** A fresh event loop per task. Connection pools are bound to a loop, so
a pool created in one task's loop is unusable in the next, meaning either a
new pool per task (expensive) or subtle "attached to a different loop" errors.
Shutdown gets harder: a SIGTERM mid-task has to unwind a loop the pool lives in.

### Option B: Make workers async (gevent, or an async worker loop)
**Pros.** One engine genuinely works.
**Cons.** gevent monkey-patches the standard library, which interacts badly
with the Docker SDK's socket handling, precisely the code path that matters
most here. And the actual workload is not I/O-concurrency-bound: it is
"supervise one container at a time", which prefork models perfectly.

### Option C: Two engines: asyncpg for the API, psycopg for workers
**Pros.** Each runtime uses the driver built for it. No event loops in workers.
Prefork children get clean process isolation.
**Cons.** Two session factories. Two connection-pool configurations. A reader
has to know which to use where.

## Decision

**Option C.** `crucible/db/session.py` exposes both.

- `get_async_engine()` / `get_db()`. FastAPI request path, asyncpg.
- `get_sync_engine()` / `sync_session_scope()`, queue worker and Alembic, psycopg.

The sync engine uses `NullPool`. This is the detail that matters: a connection
pool created before `fork()` is inherited by every child, so parent and
children share the same TCP sockets and corrupt each other's protocol state.
The symptom is intermittent "connection already closed" or "server sent data
for a query we didn't issue", and it is miserable to debug. `NullPool` opens a
connection per session and closes it after, which is the right trade when tasks
are seconds long and connections are microseconds to open.

## Consequences

### What this makes easy
- No event loop lifecycle problems in workers.
- Each pool tuned for its actual access pattern: a real pool for thousands of short web requests, no pool for long-running fork()ed tasks.
- Alembic (synchronous by nature) uses the sync engine directly with no adapters.

### What this makes hard
- Contributors must know which session to use. Mitigated by the module docstring and by services being written against one or the other explicitly, never both.
- Shared query logic used from both sides has to be written twice or kept driver-agnostic.

## In short

> The worker loop is synchronous, so I'd have to spin up an event loop
> per job just to reach the database, and connection pools are bound to a
> loop, so that gets messy fast. Two engines is simpler. The important detail
> is the worker engine uses NullPool, because a pool created before fork() is
> shared across every child process and they corrupt each other's sockets.
> That's a genuinely nasty bug to track down, so it's worth avoiding by design.
