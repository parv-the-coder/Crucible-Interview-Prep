# Architecture

## The shape

```
┌──────────────────────────────────────────────────────────────────┐
│  Browser                                                         │
│  React + Vite + Monaco                                           │
└───────┬──────────────────────────────────────┬───────────────────┘
        │ HTTPS (REST)                         │ WebSocket
        ▼                                      ▼
┌──────────────────────────────────────────────────────────────────┐
│  FastAPI (async, stateless)                                      │
│  ├ routers      auth · questions · submissions · sessions · rooms│
│  ├ services     business logic, no HTTP knowledge                │
│  └ realtime     connection hub (process-local fan-out)           │
└───────┬──────────────────────────────────────────────────────────┘
        │ SQLAlchemy async
        ▼
┌────────────────────────────────┐
│  PostgreSQL 16                 │
│  14 tables                     │
│  source of truth               │
│  submissions(queued) = the queue│
└────────▲───────────────┬───────┘
         │               │ FOR UPDATE SKIP LOCKED
         │ SQLAlchemy    ▼
         │ sync  ┌──────────────────────────────────┐
         └───────┤  queue workers (N processes)     │
                 │  ├ sandbox runs                  │
                 │  ├ LLM calls                     │
                 │  └ periodic sweeps               │
                 └───────────┬──────────────────────┘
                                │ docker.sock
                                ▼
                    ┌──────────────────────────────────┐
                    │  Sandbox containers              │
                    │  no network · caps dropped       │
                    │  read-only rootfs · pid capped   │
                    └──────────────────────────────────┘
```

## Why these boundaries

**API and worker are separate processes.** This is the split that matters, and
it is the only one the system actually needs. They have different resource
profiles: the API does millisecond work and wants concurrency, while the worker
does ten-second work and wants isolation. So they scale independently, and a
burst of submissions cannot make signing in slow.

**Everything else is one deployable.** A modular monolith: `services/` holds
business logic that knows nothing about HTTP, `api/` holds routing and
serialisation, `db/` holds models. The boundaries are real, they are just not
network boundaries.

Splitting further would buy independent scaling of components that do not need
it, and cost network calls, distributed transactions and five deployment
pipelines. Being able to say *why you did not* is worth more than having done it.

**Postgres is the only source of truth**, and now the only datastore. There is
no broker to fall out of step with it: a submission row in `queued` *is* the
pending job, so the write that records the work and the write that schedules it
are the same write. If a worker dies mid-run its row is left in `running` and
the reaper returns it to `queued`, nothing is corrupted, and a client that
reconnects replays from Postgres and is correct again. Room fan-out is the one
thing held only in memory, and losing it costs a redraw rather than data.

## Request flows

### Submitting code

```
POST /api/v1/submissions
  ├─ validate           question exists, language allowed, non-empty
  ├─ INSERT submission  status = queued
  ├─ COMMIT             ◀── the enqueue is deferred until after this
  └─ 202 { id }         measured p50 25 ms

worker
  ├─ claim              UPDATE ... WHERE status = 'queued'   (idempotent)
  ├─ load question + test cases
  ├─ run sandbox        outside the transaction
  ├─ persist results
  ├─ dispatch rating update      separate task
  └─ dispatch AI review          separate queue
```

Two details carry most of the weight. The enqueue happens **after** commit, so
a worker cannot dequeue before the row is visible. And the claim is a
**conditional UPDATE**, so duplicate delivery is harmless. Both are explained in
[07-async-evaluation](07-async-evaluation.md).

### A live interview room

```
client ──edit{version, op}──▶ API ──▶ INSERT room_events (room_id, version)
                                            │
                              ┌─────────────┴─────────────┐
                        succeeds                       fails (unique)
                              │                            │
                     broadcast to room            reply "rebase" + missed ops
                     via local sockets                     │
                     (this process only)       client rebases and retries
```

The concurrency control is a unique constraint. Postgres serialises the
writers; there are no transform functions to get wrong.

## The layers

| Layer | Knows about | Does not know about |
| --- | --- | --- |
| `api/` | HTTP, status codes, serialisation | SQL, sandboxes |
| `services/` | Business rules, the database | HTTP, request objects |
| `db/` | Schema, constraints, indexes | Business rules |
| `evaluation/` | Sandboxes, grading | HTTP, the database |
| `ai/` | Providers, prompts, budgets | Questions, submissions |
| `workers/` | Task orchestration | How grading works |

The rule that keeps this honest: **services take a session and return data, not
responses.** A service that raises `HTTPException` has leaked, and several
deliberately do, the ones where the HTTP status *is* the business rule (404 vs
403 for someone else's submission). That is a conscious exception, not drift.

## Where state lives

| State | Where | Why |
| --- | --- | --- |
| Users, questions, submissions, sessions, rooms | Postgres | Durable, relational, constrained |
| Queued jobs | Postgres (`submissions.status`) | The job and the row are one write, so they cannot disagree |
| Room fan-out | Process-local | Transport only; the durable log is in Postgres |
| WebSocket connections | API process memory | Inherently per-process |
| Access tokens | Nowhere | Stateless by design; that is the trade |
| Refresh tokens | Postgres, hashed | Must be revocable |

The API is stateless apart from live WebSocket connections, which is why it
scales horizontally and why room fan-out needs a broker at all.

## Failure behaviour

| Component down | Effect | Recovery |
| --- | --- | --- |
| Worker | Submissions queue up | Restart; the reaper requeues anything stuck |
| Postgres | Read and write both fail; readiness returns 503 | Restart; nothing lost |
| Docker daemon | Grading fails | **Does not** fall back to the unsafe sandbox in production |
| AI provider | Feedback is null | Grades are unaffected |

The last two are deliberate design decisions rather than accidents of
implementation, and both are stated in the code at the point they take effect.

## Scaling, in the order I would actually do it

1. **More workers.** Stateless; autoscale on queue depth, not CPU. Measured
   queue wait was 16 s p50 under a 25-submission burst on 3 workers, so this is
   the first thing to saturate.
2. **More API processes.** The REST path is stateless and scales now. **Rooms
   do not**, fan-out is process-local, so a second API process cannot see the
   first's room traffic. Either route rooms stickily to one process, or add
   Postgres `LISTEN/NOTIFY` fan-out, which is the intended fix and does not
   need a broker back.
3. **Read replicas** for analytics.
4. **Partition `submissions`** by month once it is genuinely large. The schema
   is already compatible.
5. **Cache hot question reads**, read-heavy and rarely changing. An
   in-process TTL cache goes a long way before a shared cache is worth a
   second datastore again.

Microservices appear nowhere on that list, and that is the point.
