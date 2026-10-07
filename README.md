# Crucible

A technical interview preparation platform. Candidates practise against timed,
proctored question sets. Their code runs in a hardened container against hidden
test cases, and an LLM reviews the solution and asks a follow-up question. A
live room lets an interviewer and a candidate share one editor in real time.

The backend is FastAPI and PostgreSQL. The frontend is React with Vite. The
design goal throughout was to take the hard parts seriously: running untrusted
code safely, grading it asynchronously without losing work, and keeping the
answer key unreachable.

---

## Features

**Question bank.** Code, SQL and multiple-choice questions, filterable by topic,
type and difficulty. Question detail responses carry sample test cases only.
Hidden cases have no serialisable shape, so they cannot leak through a response
model.

**Sandboxed execution.** Python, JavaScript, C++, Java and Go. Each run happens
in a container with no network, all Linux capabilities dropped,
`no-new-privileges`, a read-only root filesystem, a bounded `tmpfs` scratch
directory, a pid limit, swap disabled, and a non-root uid. Output is read
incrementally and capped, so a print loop cannot run the worker out of memory.

**Asynchronous grading.** `POST /submissions` validates the request, inserts a
`queued` row and returns `202` in about 25 ms at p50. Workers claim jobs with
`FOR UPDATE SKIP LOCKED` and a conditional `UPDATE`, which makes duplicate
delivery harmless. A reaper requeues rows left in `running` by a dead worker.

**Timed tests.** Server-enforced deadlines, continuously autosaved drafts (stored
on the session item, not as submissions), browser-reported proctoring with a
three-strike rule, and per-topic scoring afterwards.

**AI review.** Rubric-based feedback plus a follow-up question on graded
submissions, and an optional single hint while solving. The model never sees the
answer key and never decides the score. Every call is written to a ledger table
that enforces per-user daily budgets and makes feedback reproducible. Providers
are pluggable: Gemini, Ollama, or a deterministic fake for tests.

**Adaptive difficulty.** Elo ratings for both users and questions, updated on
every graded submission, so a question's real difficulty corrects itself from
evidence rather than from the author's label.

**Live interview rooms.** A shared Monaco editor over WebSockets with presence,
cursors, chat, and the ability to run the shared document in the same sandbox.
Concurrency control is a `UNIQUE (room_id, version)` constraint on an
append-only event log. The loser of a race is told what it missed and rebases.
Because the log is append-only, a session can be replayed.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Browser: React + Vite + Monaco                              │
└──────┬────────────────────────────────────┬──────────────────┘
       │ REST                               │ WebSocket
       ▼                                    ▼
┌──────────────────────────────────────────────────────────────┐
│  FastAPI (async, stateless)                                  │
│   api/        routing, serialisation, error envelope         │
│   services/   business logic, no HTTP knowledge              │
│   realtime/   room hub (process-local fan-out)               │
└──────┬───────────────────────────────────────────────────────┘
       │ SQLAlchemy async
       ▼
┌──────────────────────────────┐
│  PostgreSQL 16, 14 tables    │
│  source of truth, and the    │
│  job queue: a submission     │
│  row in `queued` IS the job  │
└──────▲──────────────┬────────┘
       │              │ FOR UPDATE SKIP LOCKED
       │              ▼
       │     ┌─────────────────────────────┐
       └─────┤  queue workers (N processes)│
             │  sandbox runs, LLM calls,   │
             │  periodic sweeps            │
             └──────────┬──────────────────┘
                        │ docker.sock
                        ▼
             ┌─────────────────────────────┐
             │  sandbox containers         │
             │  no network, caps dropped,  │
             │  read-only rootfs, pids cap │
             └─────────────────────────────┘
```

There are two processes that matter. The API does millisecond work and wants
concurrency; the worker does ten-second work and wants isolation. They scale
independently, so a burst of submissions cannot make signing in slow. Everything
else is a modular monolith with real boundaries that are not network boundaries.

Postgres is the only datastore. There is no broker that can fall out of step
with the data, because the write that records the work and the write that
schedules it are the same write.

### Stack

| | |
| --- | --- |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2 (async and sync), Alembic, Pydantic v2 |
| Database | PostgreSQL 16, used for state and for the job queue |
| Sandbox | Docker via the `docker` SDK, with a dev-only `subprocess` fallback |
| Auth | Argon2id passwords, JWT access tokens, rotating refresh tokens |
| AI | Google Gemini or Ollama, behind a provider interface |
| Frontend | React 18, TypeScript, Vite, React Router, Monaco |
| Tooling | ruff, mypy (strict), pytest, structlog |

---

## Getting started

### Requirements

| | Why |
| --- | --- |
| Docker 24+ | Postgres and the execution sandbox |
| Python 3.12+ | `StrEnum`, PEP 695 generics |
| Node 20+ | Frontend only |
| ~4 GB disk | Language images (python, node, gcc) |

On Linux you must be in the `docker` group, and that only takes effect on a new
login:

```bash
sudo usermod -aG docker $USER
newgrp docker                 # or log out and back in
docker run --rm hello-world   # must succeed before continuing
```

The API and worker inherit their groups from the shell that launched them. If
they cannot reach the Docker daemon they fall back to the insecure sandbox. That
is a warning in `local` and a refusal to start anywhere else.

### First run

```bash
cp .env.example .env

# 1. database
docker compose -f infra/docker-compose.yml up -d

# 2. backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e "backend[dev]"

cd backend
alembic upgrade head
python -m crucible.scripts.seed

# 3. run it, one terminal each
uvicorn crucible.main:app --reload --port 8000
python -m crucible.workers.runner        # run N times for N workers

# 4. frontend
cd frontend && npm install && npm run dev
```

Then:

- Frontend: <http://localhost:5173>
- API docs: <http://localhost:8000/docs> (disabled in production)
- Readiness: <http://localhost:8000/health/ready>

Seeded accounts:

| Role | Email | Password |
| --- | --- | --- |
| admin | `admin@crucible.dev` | `admin-password-123` |
| interviewer | `interviewer@crucible.dev` | `interviewer-pass-123` |
| student | `student@crucible.dev` | `student-password-123` |

### Check it actually works

`GET /health/ready` reports the sandbox backend:

```json
{
  "status": "ready",
  "checks": {
    "database": { "ok": true, "latency_ms": 3.1 },
    "sandbox":  { "ok": true, "backend": "docker", "production_safe": true, "warnings": [] }
  }
}
```

If `backend` reads `"subprocess"` with `production_safe: false`, the process
could not reach Docker and fell back to the insecure executor. That is almost
always the group membership problem above.

### Makefile

`make help` lists everything. The ones used most:

| | |
| --- | --- |
| `make up` / `make down` | Start or stop Postgres |
| `make api` / `make worker` | Run the API or a queue worker |
| `make migrate` / `make seed` | Apply migrations or load the question bank |
| `make test` | Unit and integration tests |
| `make test-sandbox` | Real attack cases against real containers |
| `make check` | Lint plus unit tests, which is what CI runs |
| `make images` | Pre-pull the language images |
| `make doctor` | Diagnose a broken local setup |
| `make reap` | Destroy sandbox containers left by a crashed worker |

---

## Running without Docker

The platform runs, with one significant caveat:

```bash
SANDBOX_BACKEND=subprocess uvicorn crucible.main:app --reload
```

The subprocess backend applies POSIX rlimits and a scratch directory. That
contains an honest program. It does not contain a hostile one: there is no
filesystem, network or PID namespace, and rlimits are per-process, so a fork
bomb evades them. It exists so the project runs without a container runtime and
so the unit suite needs no daemon. Do not use it outside local development. The
application refuses to start with it in any other environment.

You still need Postgres. Install it natively and point `.env` at it.

---

## Configuration

Every setting comes from the environment and is validated at import, so a
misconfigured deployment fails at boot rather than at the first request that
touches the bad value. `.env.example` has the full list. The ones that matter:

| Variable | Default | Notes |
| --- | --- | --- |
| `ENVIRONMENT` | `local` | Outside local and test, weak JWT secrets and the insecure sandbox are refused |
| `JWT_SECRET` | dev default | Must be at least 32 bytes outside local. Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `SANDBOX_BACKEND` | `docker` | `subprocess` is dev-only |
| `SANDBOX_TIMEOUT_SECONDS` | `10` | Wall-clock cap per run |
| `SANDBOX_MEMORY_MB` | `256` | Swap is pinned to the same value |
| `SANDBOX_PIDS_LIMIT` | `64` | The fork bomb control |
| `SANDBOX_POOL_ENABLED` | `true` | Warm containers. Set `false` for genuinely adversarial users |
| `SANDBOX_ENABLED_LANGUAGES` | `python,javascript,cpp` | Which images are pre-pulled and pooled |
| `AI_ENABLED` | `true` | AI features degrade to `null`. Grading is unaffected |
| `AI_PROVIDER` | `gemini` | `gemini`, `ollama` or `fake` |
| `AI_DAILY_BUDGET_PER_USER` | `50` | Counted from the `ai_interactions` ledger |

---

## API

Versioned under `/api/v1`. Full interactive docs at `/docs`.

| Area | Endpoints |
| --- | --- |
| Auth | `POST /auth/signup`, `/signin`, `/refresh`, `/signout`, `/signout-all`; `GET /auth/me` |
| Questions | `GET /questions`, `/questions/topics`, `/questions/{id}`; `POST`, `PATCH`, `DELETE` for admins, where delete archives rather than hard-deletes |
| Submissions | `POST /submissions` (202), `GET /submissions`, `GET /submissions/{id}`, `POST /submissions/hint`, `GET /submissions/languages` |
| Sessions | `POST /sessions`, `GET /sessions/{id}`, `PUT /sessions/{id}/items/{item}/draft`, `POST /sessions/{id}/violations`, `POST /sessions/{id}/submit`, `GET /sessions/{id}/result` |
| Rooms | `POST /rooms`, `POST /rooms/join`, `GET /rooms/{id}/replay`, `POST /rooms/{id}/end`, `PUT /rooms/{id}/feedback`, `WS /ws/rooms/{id}` |
| Operational | `GET /health/live`, `GET /health/ready` |

### Conventions

**One error envelope, always.** Validation errors, `HTTPException`s and
unhandled exceptions all serialise to the same shape, so clients never have to
branch on which layer failed:

```json
{
  "error": { "code": "language_not_allowed",
             "message": "This question accepts: python, javascript",
             "field": "language" },
  "request_id": "9f2c1a..."
}
```

`request_id` is echoed in the response header and appears on every log line for
that request. Internal messages are suppressed outside debug mode.

**`202`, not `200`, for submissions.** The work has not happened yet, and a
score of zero would be a lie that clients then have to detect. The response
carries a `poll_url`.

**`404`, not `403`, for someone else's resource.** A `403` confirms the id is
real, which turns id enumeration into a discovery tool.

**`Idempotency-Key` on submissions.** A sparse unique index on
`(user_id, idempotency_key)` means a retried POST returns the original instead
of paying for a second sandbox run. The pre-check races, but the index does not,
so the `IntegrityError` path returns the winner.

**`limit + 1`, not `COUNT(*)`.** Listings fetch one extra row and report
`has_more`. Counting a filtered set is the expensive half of a listing query,
and most UIs only need to know whether a next page exists.

WebSockets sit outside `/api/v1` on purpose. The protocol is negotiated on the
frame, not the path.

---

## Data model

14 tables and 11 native enum types. The rule throughout is to make bad states
unrepresentable in the database rather than merely unlikely in the application.

```
users ──┬──< refresh_tokens          (rotation families)
        ├──< topic_mastery           (per-topic Elo)
        ├──< test_sessions ──< session_items >── questions ──< test_cases
        │         └──< violations                    │              │
        ├──< submissions >───────────────────────────┘              │
        │         └──< submission_results >─────────────────────────┘
        ├──< interview_rooms ──< room_participants
        │         └──< room_events   (append-only op log)
        └──< ai_interactions         (audit and cost ledger)
```

Decisions that apply everywhere:

- **UUID primary keys**, so ids can be minted client-side, and because
  sequential integers leak activity volume.
- **`timestamptz` defaulted server-side**, so clock authority stays with
  Postgres.
- **Native enums pinned to store values, not member names.** SQLAlchemy stores
  the member name by default, which silently breaks any partial index whose
  predicate uses the lowercase value.
- **An explicit constraint naming convention**, so a downgrade can drop what
  autogenerate created.
- **An `ON DELETE` clause on every foreign key**, enforced by a test.

Indexes are partial where the useful set is small. The stuck-job reaper scans
`(started_at) WHERE status IN ('queued','running')`, so its index stays tiny no
matter how much submission history accumulates.

---

## Security

The platform runs code written by people we do not trust, on our own hardware.
That is structurally the same situation as a remote code execution
vulnerability, and the only thing separating a code judge from one is the
quality of the confinement. So the sandbox is layered on the assumption that any
single control will eventually fail:

| Control | What it stops |
| --- | --- |
| `network_disabled` | Exfiltration, reverse shells, mining, and using our IP to attack third parties |
| `cap_drop=["ALL"]` | Container escape and privilege escalation. A program that reads stdin and writes stdout needs none of the default capabilities |
| `no-new-privileges` | Regaining privileges through a setuid binary in the base image |
| `read_only` rootfs | Persistence. Without it, code could overwrite a stdlib file and affect the next run in a pooled container |
| `tmpfs` at 32 MB, `noexec`, `nosuid`, `nodev` | Disk exhaustion, and executing a written binary. `exec` is granted only to compiled languages, which must run their own artefact |
| `pids_limit=64` | Fork bombs. `fork()` returns `EAGAIN` at the 65th process |
| `mem_limit == memswap_limit` | Memory exhaustion. Setting the memory limit alone is not enough, because Docker defaults swap to twice that value |
| `user="65534:65534"` | Everything requiring root, as the backstop that assumes every layer above it failed |
| rlimits (`nofile`, `fsize`, `core`) | Exhausting host file descriptors, writing a huge file into the tmpfs, dumping core to disk |
| Bounded output reader | Output floods. The bytes are streamed out of the container, so a container memory limit does not help. The supervisor stops storing after 64 KB but keeps draining the pipe |

Everything else:

- **Argon2id for passwords, not bcrypt.** bcrypt truncates input at 72 bytes and
  is memory-light, which is the property GPU cracking rigs exploit. Parameters
  target 50 to 100 ms per hash.
- **15-minute access tokens and 14-day rotating refresh tokens.** Every refresh
  revokes the token presented, so a refresh token is single-use and replay is
  detectable. On replay the whole token family is revoked.
- **Algorithm pinned at decode** (`algorithms=["HS256"]`, never read from the
  token header), token type checked as a claim, issuer checked.
- **Refresh tokens stored as SHA-256**, not Argon2. The input is 300+ bits of our
  own entropy, so there is no dictionary to attack and the lookup should be fast.
- **No enumeration via sign-in.** One message for both failure modes, and
  equivalent CPU burned when the account does not exist, so response latency
  does not reveal valid email addresses. A test asserts the two paths stay within
  an order of magnitude.
- **Answer-key containment is structural.** Question payloads are filtered by an
  allow-list, and no response model can represent a hidden test case.
- **OpenAPI is disabled in production.** A schema is a map of the attack surface.

Known gaps before anything resembling production:

1. Secrets belong in a secrets manager, not a `.env` file.
2. Submission creation is unmetered per user, and the sandbox is the most
   expensive resource in the system.
3. The worker's access to the Docker socket is equivalent to root on the host,
   so it should be the only thing that can reach it, ideally through rootless
   Docker or a dedicated executor service.

---

## Tests

```bash
cd backend
pytest tests/unit         # no external dependencies
pytest -m integration     # needs Postgres
pytest -m sandbox         # needs Docker, runs real attack cases
pytest --cov=crucible     # coverage
```

140 test functions: 81 unit and 59 integration. The `sandbox` suite is the one
that actually proves the security controls, with fork bombs, memory bombs,
output floods and escape attempts against real containers. The unit suite
exercises the same logic through the subprocess backend, which approximates but
cannot reproduce kernel-level behaviour.

Linting and types are `ruff check`, `ruff format` and `mypy --strict`.
`make check` is what CI runs.

---

## Layout

```
backend/
  crucible/
    api/            routers, error handlers, middleware, WebSocket entrypoint
    core/           config, logging, security primitives
    db/             models, enums, session factories
    evaluation/
      sandbox/      docker and subprocess backends, language profiles
      strategies/   code, sql and mcq graders behind one interface
    ai/             provider interface, Gemini, Ollama, fake, prompts
    realtime/       connection hub and wire protocol
    services/       business logic: auth, questions, submissions, sessions, rooms, adaptive
    workers/        queue, task definitions, worker runner
    scripts/        seed data
  alembic/          migrations
  tests/            unit and integration
frontend/
  src/
    api/            typed client and response types
    auth/           auth context
    components/     editor, layout, result panel
    hooks/          autosave, countdown, proctoring, room socket, submission polling
    pages/          sign-in, questions, solve, test, rooms, history
infra/              docker-compose for the local datastore
```

The layering rule that keeps `services/` honest: a service takes a session and
returns data, not responses. The few deliberate exceptions are the places where
the HTTP status is itself the business rule, such as 404 instead of 403 for
someone else's submission.

---

## Troubleshooting

**Submissions stay `queued` forever.** No worker is running. There is no separate
enqueue step that can fail, because the row is the queue entry, so a queued row
with no worker means exactly one thing. `make doctor` prints the queue depth. If
a worker is running and the depth still grows, look in its log for
`worker.claim_failed` (the database is unreachable from the worker) or
`submission.evaluation_error` (the sandbox is broken).

**`sandbox: subprocess` when you expect `docker`.** Group membership, as
described under Requirements. The API and worker inherit groups from their
launching shell.

**Alembic autogenerates an empty migration.** A new model was not imported in
`crucible/db/models/__init__.py`. Autogenerate walks `Base.metadata` and cannot
see a model that nothing imported.

**Sandbox containers accumulate after a crash.** A clean shutdown destroys the
pool, but a SIGKILL cannot. `make reap` clears them, and the worker reaps
orphans on start. Inspect with `docker ps -a --filter label=crucible.sandbox=1`.
