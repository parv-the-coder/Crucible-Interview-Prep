# Running Crucible

## What you need

| Requirement | Why |
| --- | --- |
| Docker 24+ | Postgres and the code-execution sandbox |
| Python 3.12+ | The backend uses PEP 695 generics and `enum.StrEnum` |
| Node 20+ | Frontend only |
| ~4 GB free disk | Language images (python, node, gcc) |

**Linux note.** After installing Docker you must be in the `docker` group, and
that only takes effect on a new login:

```bash
sudo usermod -aG docker $USER
# then log out and back in, or for the current shell only:
newgrp docker
docker run --rm hello-world     # must succeed before continuing
```

If `docker run` fails with "permission denied ... /var/run/docker.sock", the
group has not been applied to your session yet.

---

## First run

```bash
git clone <your-repo-url> crucible && cd crucible
cp .env.example .env

# 1. datastores
docker compose -f infra/docker-compose.yml up -d
docker compose -f infra/docker-compose.yml ps      # both should be (healthy)

# 2. backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e "backend[dev]"

cd backend
alembic upgrade head
python -m crucible.scripts.seed

# 3. run it (two terminals, or use the Makefile targets)
uvicorn crucible.main:app --reload --port 8000
python -m crucible.workers.runner        # run this N times for N workers
```

Then:

- API docs, <http://localhost:8000/docs>
- Readiness, <http://localhost:8000/health/ready>

Seeded accounts:

| Role | Email | Password |
| --- | --- | --- |
| admin | `admin@crucible.dev` | `admin-password-123` |
| interviewer | `interviewer@crucible.dev` | `interviewer-pass-123` |
| student | `student@crucible.dev` | `student-password-123` |

## Verify it actually works

`/health/ready` is the fastest check, and the field to look at is the sandbox
backend:

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
could not reach the Docker daemon and fell back to the **insecure** local
executor. In `local` that is a warning; in any other environment the process
refuses to start rather than run untrusted code unconfined.

The usual cause is the group membership problem above, the API and worker
inherit their groups from the shell that launched them.

## Running without Docker

The platform runs, with one significant caveat.

```bash
SANDBOX_BACKEND=subprocess uvicorn crucible.main:app --reload
```

The subprocess backend applies POSIX rlimits and a scratch directory, which
contains an *honest* program. It does not contain a hostile one: no filesystem
namespace (submitted code can read your files, including `.env`), no network
namespace, no PID namespace, and rlimits are per-process so a fork bomb evades
them. It exists so the project runs on a laptop without a container runtime and
so the test suite needs no daemon.

You still need Postgres. Without Docker, install it natively and point `.env`
at it.

## Tests

```bash
cd backend
pytest tests/unit                 # no external dependencies, ~30s
pytest -m integration             # needs Postgres
pytest -m sandbox                 # needs Docker; runs real attack cases
pytest --cov=crucible             # coverage
```

`-m sandbox` is the suite that actually proves the security controls: fork
bombs, memory bombs, output floods and container escapes, against real
containers. The unit suite exercises the same logic through the subprocess
backend, which approximates but cannot reproduce kernel-level behaviour.

## Configuration

Every setting is environment-driven and validated at import. A misconfigured
deployment fails at boot rather than at the first request that touches the bad
value. See `.env.example` for the full list; the ones that matter most:

| Variable | Default | Notes |
| --- | --- | --- |
| `ENVIRONMENT` | `local` | Outside local/test, weak JWT secrets and the insecure sandbox are refused |
| `JWT_SECRET` | dev default | Must be ≥32 bytes in production (RFC 7518 §3.2). `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `SANDBOX_BACKEND` | `docker` | `subprocess` is dev-only |
| `SANDBOX_POOL_ENABLED` | `true` | Set `false` for genuinely adversarial users, see ADR-0006 |
| `SANDBOX_ENABLED_LANGUAGES` | `python,javascript,cpp` | Which images are pre-pulled and pooled |
| `WORKER_CONCURRENCY` | `4` | Concurrent sandbox executions per worker |
| `AI_ENABLED` | `true` | AI features degrade to null; grading is unaffected |

## Production considerations

The full list of what this does not yet do is in
[15-limitations.md](15-limitations.md). The three that matter most before
anything resembling production:

1. **Secrets belong in a secrets manager**, not a `.env` file.
2. **Rate limiting on submission creation.** The sandbox is the most expensive
   resource in the system and is currently unmetered per user.
3. **The worker's Docker socket access is equivalent to root on the host.**
   In production the worker should be the only thing that can reach it, ideally
   via rootless Docker or a dedicated executor service.

## Troubleshooting

**Submissions stay `queued` forever.**
No worker is running. There is no separate enqueue step that can fail, the row
*is* the queue entry, so a queued row with no worker means exactly one thing.
Check the depth with SQL:

```sql
SELECT count(*) FROM submissions WHERE status = 'queued';
```

`make doctor` prints the same number. If a worker *is* running and the depth
still grows, look for `worker.claim_failed` (the database is unreachable from
the worker) or `submission.evaluation_error` (the sandbox is broken) in its
log.

**`sandbox: subprocess` when you expect `docker`.**
Group membership (see above). The API and worker inherit groups from their
launching shell.

**Alembic autogenerates an empty migration.**
A new model was not imported in `crucible/db/models/__init__.py`. Autogenerate
walks `Base.metadata` and cannot see a model nothing imported.

**Containers accumulate after a worker crash.**
`docker ps -a --filter label=crucible.sandbox=1`. Clean shutdown destroys the
pool; a SIGKILL cannot. `DockerSandbox.reap_orphans()` clears them and runs on
worker start.
