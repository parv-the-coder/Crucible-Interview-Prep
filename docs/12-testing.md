# Testing

161 tests: 102 unit, 59 integration. The split is not about speed, it is about
**what can only be proven against real infrastructure**.

```bash
pytest tests/unit          # no dependencies, ~40s
pytest -m integration      # needs Postgres
pytest -m sandbox          # needs Docker; runs real attacks
```

## The strategy

**Unit tests cover logic that has a right answer.** Scoring maths, Elo
expectations, output normalisation, token encoding, schema invariants. Fast,
deterministic, no daemon.

**Integration tests cover things that are only true in a real system.** This is
the important category, and it is where most of this project's bugs actually
lived:

| Bug | Why unit tests could never have caught it |
| --- | --- |
| Token revocation rolled back | Only exists at a transaction boundary. A mocked session shows it working perfectly |
| `put_archive` on a read-only container | A Docker API rule, not application logic |
| Fork bomb poisoning a pooled container | Needs a real pids cgroup |
| Sticky `OOMKilled` flag | Container-lifetime state |
| Cleanup killing its own container | Needs a real PID 1 |

Seven of twelve documented bugs only appeared against real infrastructure. That
ratio is the argument for the integration suite existing at all.

## What the tests actually assert

**Security tests execute the attacks.** Not "is `pids_limit` set" but "run a
fork bomb and assert the host survives":

```python
def test_fork_bomb_is_refused(docker_sandbox):
    result = run(docker_sandbox, "import os\nwhile True:\n    os.fork()", timeout=5)
    assert result.outcome in (RUNTIME_ERROR, TIMEOUT)
    assert result.duration_ms < 8000
```

Also covered: memory bombs, output floods, filesystem reads, network access,
container writes, orphaned processes, `alg=none` tokens, forged signatures,
refresh replay, and timing equalisation.

**Tests assert properties, not mechanisms.** The orphan test checks nothing
survives the run, either `RLIMIT_NPROC` refusing the fork or the timeout
killing the process group is a pass. Asserting *which* one would make the test
brittle without making the system safer.

**Schema invariants are tested.** A test walks every index predicate in the
metadata and asserts each string literal exists in its enum. That is what makes
the silent dead-index bug ([#1](14-bugs-found.md)) a class of bug that cannot
recur, rather than a one-line fix. Also enforced: every constraint is named,
every foreign key declares `ON DELETE`, every model is exported where Alembic
can see it.

## Things learned the hard way

**Exact frame counts, never `receive()` in a loop.** The first WebSocket tests
read "up to 4 frames looking for the one I want". When the server sends fewer,
that **blocks forever**, a hang, not a failure, and far worse to debug. The
server sends a known sequence, so the tests expect exactly that sequence.

**`NullPool` under test.** An asyncpg pool is bound to the event loop that
created it, and the suite legitimately runs two, pytest-asyncio's, and the one
Starlette's `TestClient` starts to drive WebSockets. A pooled connection created
in one and reused in the other fails with "attached to a different loop", which
looks like a bug in the code under test and is not.

**Test cleanup can deadlock with a live worker.** Deleting a user cascades into
submissions the worker is grading, in the opposite lock order. The fixture now
retries, and the underlying lock ordering is a real property of the system,
not a test artefact ([#11](14-bugs-found.md)).

**A fake AI provider, not mocks.** `FakeProvider` returns schema-valid output
derived from a prompt hash, so it is deterministic and the entire AI layer runs
offline for free. It can also be told to fail, which is how the degradation
path is tested. Mocking the SDK would test the mock.

## What is deliberately not tested

Being explicit, because unstated gaps read as oversights.

- **No frontend tests.** The valuable ones would be end-to-end (Playwright)
  rather than component tests, and that is real setup. The typecheck is strict
  and catches the class of error most likely here.
- **No load tests.** So there is no honest concurrent-user number, and
  [11-performance](11-performance.md) says so.
- **No mutation testing.** Would be a genuinely good next step for the scoring
  logic, where a wrong-but-passing test is plausible.
- **No property-based tests.** Output normalisation and the edit-op rebase are
  the two places Hypothesis would earn its keep.

## Coverage

Coverage is measured but not gated on a number. A percentage target pushes
people toward testing getters, and the tests that matter here, a fork bomb, a
replayed token, are worth more than the fifty lines of serialisation code they
do not touch.

The question I ask instead: *if this line were wrong, would a test fail?* For
the sandbox, the auth flow and the scoring logic, yes.

## CI

Not yet wired up, which is a real gap. What it should run:

```
ruff check + ruff format --check
mypy
pytest tests/unit
docker compose up -d && pytest -m integration
cd frontend && npm ci && npm run build
```

The integration suite needs Postgres and Docker, both of which GitHub
Actions provides via service containers.
