# Architecture Decision Records

An ADR captures one significant decision at the moment it was made: what forced
the choice, what the options were, which one was taken, and what it cost.

They are immutable. If a decision is reversed, the old ADR is marked superseded
and a new one explains why, the history of a system's thinking is more useful
than a tidy snapshot of its conclusions.

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-rewrite-in-python.md) | Rewrite in Python rather than incrementally refactoring the Node service | Accepted |
| [0002](0002-postgres-over-mongodb.md) | PostgreSQL instead of MongoDB | Accepted |
| [0003](0003-two-database-engines.md) | Separate async and sync SQLAlchemy engines | Accepted |
| [0004](0004-celery-over-arq.md) | Celery instead of ARQ, Dramatiq or RQ | Superseded by 0011 |
| [0005](0005-container-sandbox.md) | Docker containers for code execution, behind an interface | Accepted |
| [0006](0006-warm-container-pool.md) | Warm container pool, with bounded reuse | Accepted |
| [0007](0007-jwt-with-rotating-refresh.md) | Stateless access tokens + rotating refresh tokens | Accepted |
| [0008](0008-provider-agnostic-ai.md) | Abstract the LLM provider rather than calling one SDK directly | Accepted |
| [0009](0009-operation-log-for-rooms.md) | Versioned operation log for collaborative editing, not a CRDT | Accepted |
| [0010](0010-elo-for-adaptive-difficulty.md) | Elo rating for adaptive question selection, not full IRT | Accepted |
| [0011](0011-postgres-queue-over-celery.md) | A Postgres-backed queue instead of Celery and Redis | Accepted |

## Template

```markdown
# ADR-NNNN: <decision>

Status: Proposed | Accepted | Superseded by ADR-XXXX
Date: YYYY-MM-DD

## Context
What situation forces a decision? What constraints are real?

## Options considered
### Option A
How it works. Pros. Cons.
### Option B
...

## Decision
What we chose, and the reasoning that actually decided it.

## Consequences
### What this makes easy
### What this makes hard
### What we will have to revisit
```
