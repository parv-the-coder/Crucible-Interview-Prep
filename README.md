# Crucible

A technical interview preparation platform. Candidates practise against timed,
proctored question sets. Their code runs in a hardened container against hidden
test cases, and an LLM reviews the solution and asks a follow-up question. A
live room lets an interviewer and a candidate share one editor in real time.

The backend is FastAPI and PostgreSQL; the frontend is React with Vite. The
design goal throughout was to take the hard parts seriously: running untrusted
code safely, grading it asynchronously without losing work, and keeping the
answer key unreachable.

Full engineering documentation is in [`docs/`](docs/).

---

## What it does

- **Question bank** across code, SQL and multiple choice, filterable by topic,
  type and difficulty. Question responses carry sample test cases only; hidden
  cases have no serialisable shape, so they cannot leak through a response model.
- **Sandboxed execution** for Python, JavaScript, C++, Java and Go, each run in
  a container with no network, no capabilities, a read-only root filesystem, a
  pid limit and a memory cap.
- **Asynchronous grading.** The API validates, inserts a `queued` row and
  returns `202`. Workers claim jobs and grade them out of band.
- **Timed tests** with server-enforced deadlines, autosaved drafts,
  browser-reported proctoring and per-topic scoring.
- **AI review.** Rubric-based feedback and a follow-up question on graded
  submissions. The model never sees the answer key and never decides the score.
- **Adaptive difficulty** through Elo ratings on both users and questions, so a
  question's real difficulty corrects itself from evidence.
- **Live interview rooms**: a shared Monaco editor over WebSockets with
  presence, chat, and the ability to run the shared document in the sandbox.

## How it fits together

```
Browser (React + Vite + Monaco)
   │  REST                    │  WebSocket
   ▼                          ▼
FastAPI (async, stateless)
   │  SQLAlchemy
   ▼
PostgreSQL 16 ──── state, and the job queue:
   ▲                a submission row in `queued` IS the job
   │  FOR UPDATE SKIP LOCKED
   │
queue workers (N processes) ──docker.sock──▶ sandbox containers
```

Two processes matter. The API does millisecond work and wants concurrency; the
worker does ten-second work and wants isolation. They scale independently, so a
burst of submissions cannot make signing in slow. Everything else is a modular
monolith.

Postgres is the only datastore, so there is no broker that can fall out of step
with the data: the write that records the work and the write that schedules it
are the same write.

More detail: [architecture](docs/02-architecture.md),
[async evaluation](docs/07-async-evaluation.md),
[data model](docs/03-data-model.md).

### Stack

| | |
| --- | --- |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Pydantic v2 |
| Database | PostgreSQL 16, used for state and for the job queue |
| Sandbox | Docker, with a dev-only `subprocess` fallback |
| Auth | Argon2id passwords, JWT access tokens, rotating refresh tokens |
| AI | Google Gemini or Ollama, behind a provider interface |
| Frontend | React 18, TypeScript, Vite, React Router, Monaco |
| Tooling | ruff, mypy (strict), pytest, structlog |

---

## Getting started

You need Docker 24+, Python 3.12+, Node 20+ and about 4 GB of disk for the
language images.

On Linux you must be in the `docker` group, and that only takes effect on a new
login:

```bash
sudo usermod -aG docker $USER
newgrp docker                 # or log out and back in
docker run --rm hello-world   # must succeed before continuing
```

The API and worker inherit their groups from the shell that launched them. If
they cannot reach the Docker daemon they fall back to the insecure sandbox,
which is a warning in `local` and a refusal to start anywhere else.

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

Then open the frontend at <http://localhost:5173>, the API docs at
<http://localhost:8000/docs>, and readiness at
<http://localhost:8000/health/ready>.

Seeded accounts:

| Role | Email | Password |
| --- | --- | --- |
| admin | `admin@crucible.dev` | `admin-password-123` |
| interviewer | `interviewer@crucible.dev` | `interviewer-pass-123` |
| student | `student@crucible.dev` | `student-password-123` |

`/health/ready` is the quickest way to confirm the setup. If it reports
`"backend": "subprocess"` with `production_safe: false`, the process could not
reach Docker and fell back to the insecure executor, which is almost always the
group membership problem above.

Setup, configuration and troubleshooting in full:
[13-deployment](docs/13-deployment.md).

### Common tasks

`make help` lists everything. The ones used most:

| | |
| --- | --- |
| `make up` / `make down` | Start or stop Postgres |
| `make api` / `make worker` | Run the API or a queue worker |
| `make migrate` / `make seed` | Apply migrations or load the question bank |
| `make test` | Unit and integration tests |
| `make test-sandbox` | Real attack cases against real containers |
| `make check` | Lint plus unit tests, which is what CI would run |
| `make images` | Pre-pull the language images |
| `make doctor` | Diagnose a broken local setup |
| `make reap` | Destroy sandbox containers left by a crashed worker |

### Running without Docker

```bash
SANDBOX_BACKEND=subprocess uvicorn crucible.main:app --reload
```

The subprocess backend applies POSIX rlimits and a scratch directory, which
contains an honest program but not a hostile one: there is no filesystem,
network or PID namespace, and rlimits are per-process, so a fork bomb evades
them. It exists so the project runs without a container runtime and so the unit
suite needs no daemon. The application refuses to start with it outside local
development. You still need Postgres.

---

## Configuration

Every setting comes from the environment and is validated at import, so a
misconfigured deployment fails at boot rather than at the first request that
touches the bad value. `.env.example` has the full list. The ones that matter:

| Variable | Default | Notes |
| --- | --- | --- |
| `ENVIRONMENT` | `local` | Outside local and test, weak JWT secrets and the insecure sandbox are refused |
| `JWT_SECRET` | dev default | At least 32 bytes outside local. Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
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

Versioned under `/api/v1`, with interactive docs at `/docs` outside production.

| Area | Endpoints |
| --- | --- |
| Auth | `POST /auth/signup`, `/signin`, `/refresh`, `/signout`, `/signout-all`; `GET /auth/me` |
| Questions | `GET /questions`, `/questions/topics`, `/questions/{id}`; `POST`, `PATCH`, `DELETE` for admins |
| Submissions | `POST /submissions`, `GET /submissions`, `GET /submissions/{id}`, `POST /submissions/hint`, `GET /submissions/languages` |
| Sessions | `POST /sessions`, `GET /sessions/{id}`, `PUT /sessions/{id}/items/{item}/draft`, `POST /sessions/{id}/violations`, `POST /sessions/{id}/submit`, `GET /sessions/{id}/result` |
| Rooms | `POST /rooms`, `POST /rooms/join`, `GET /rooms/{id}/replay`, `POST /rooms/{id}/end`, `PUT /rooms/{id}/feedback`, `WS /ws/rooms/{id}` |
| Operational | `GET /health/live`, `GET /health/ready` |

Conventions worth knowing before you write a client:

- **One error envelope, always.** Validation errors, `HTTPException`s and
  unhandled exceptions all serialise to `{ "error": { code, message, field },
  "request_id" }`, so clients never branch on which layer failed. The
  `request_id` is echoed in a response header and appears on every log line for
  that request.
- **`202`, not `200`, for submissions**, because the work has not happened yet.
  The response carries a `poll_url`.
- **`404`, not `403`, for someone else's resource**, since a `403` confirms the
  id is real.
- **`Idempotency-Key` on submissions**, so a retried POST returns the original
  instead of paying for a second sandbox run.
- **`limit + 1`, not `COUNT(*)`**, for listings, which report `has_more`.

Full reasoning: [04-api-design](docs/04-api-design.md).

---

## Security

Running code written by people you do not trust is structurally the same
situation as a remote code execution vulnerability, and the only thing
separating a code judge from one is the quality of the confinement. The sandbox
is therefore layered on the assumption that any single control will eventually
fail: no network, all capabilities dropped, `no-new-privileges`, a read-only
root filesystem, a bounded `noexec` tmpfs, a pid limit that stops fork bombs,
swap pinned equal to the memory limit, a non-root uid, rlimits, and an output
reader that keeps draining the pipe but stops storing after 64 KB.

Elsewhere: Argon2id password hashing, 15-minute access tokens with 14-day
rotating refresh tokens and reuse detection, the JWT algorithm pinned at decode,
sign-in that does not leak whether an account exists, and an allow-list filter
on question payloads so a new field cannot become an answer leak by default.

Details and the threat model: [05-security](docs/05-security.md) and
[06-sandbox-deep-dive](docs/06-sandbox-deep-dive.md). The honest list of gaps,
including the lack of per-user rate limiting and the worker's Docker socket
access, is in [15-limitations](docs/15-limitations.md).

---

## Tests

```bash
cd backend
pytest tests/unit         # no external dependencies
pytest -m integration     # needs Postgres
pytest -m sandbox         # needs Docker, runs real attack cases
pytest --cov=crucible     # coverage
```

161 tests: 102 unit and 59 integration. The `sandbox` suite is the one that
proves the security controls, with fork bombs, memory bombs, output floods and
escape attempts against real containers. Linting and types are
`ruff check`, `ruff format` and `mypy --strict`.

More: [12-testing](docs/12-testing.md).

---

## Layout

```
backend/
  crucible/
    api/            routers, error handlers, middleware, WebSocket entrypoint
    core/           config, logging, security primitives
    db/             models, enums, session factories
    evaluation/     sandbox backends and language profiles; code, sql, mcq graders
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
docs/               engineering documentation and ADRs
```

The layering rule that keeps `services/` honest: a service takes a session and
returns data, not responses. The few deliberate exceptions are the places where
the HTTP status is itself the business rule, such as 404 instead of 403 for
someone else's submission.

---

## Documentation

[`docs/`](docs/) has the full set. The entry points:

| | |
| --- | --- |
| [00-project-overview](docs/00-project-overview.md) | What it does and the shape of the system |
| [02-architecture](docs/02-architecture.md) | Components, flows, failure behaviour, scaling order |
| [06-sandbox-deep-dive](docs/06-sandbox-deep-dive.md) | Every sandbox control and the attack it blocks |
| [07-async-evaluation](docs/07-async-evaluation.md) | Queue semantics, idempotency, crash recovery |
| [13-deployment](docs/13-deployment.md) | Setup, configuration, troubleshooting |
| [14-bugs-found](docs/14-bugs-found.md) | Twelve real defects, with root causes and fixes |
| [15-limitations](docs/15-limitations.md) | What is missing and what to fix first |
| [adr/](docs/adr/) | Decision records, each naming the rejected alternatives |
