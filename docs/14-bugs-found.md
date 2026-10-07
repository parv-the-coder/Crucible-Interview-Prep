# Bugs found while building this

Twelve real defects hit during development, each with its symptom, root cause,
fix, and the test or structural change that stops it recurring. Every one is
fixed in the git history with a commit message explaining it.

They are worth reading as a group because of the pattern: five failed silently,
seven only appeared against real infrastructure rather than mocks, and three
were fixed incorrectly on the first attempt.

---

## 1. Partial indexes that could never match a row

**Symptom:** none. Everything worked. That is what makes it interesting.

**What happened.** SQLAlchemy persists Python enum **names** by default, not
values. `SessionStatus.ACTIVE` was stored as `'ACTIVE'`. But the partial
indexes were written against the values:

```sql
CREATE INDEX ix_test_sessions_expiring ON test_sessions (ends_at)
WHERE status = 'active';
```

An index whose predicate matches nothing is **not an error**. Postgres builds
it happily. Every query the index was designed for silently falls back to a
sequential scan, forever, and the only symptom is that the system is slower
than it should be, at a scale where you would not notice.

**How it was found.** Ruff flagged `UP042` (`class X(str, Enum)` should be
`StrEnum`). Migrating the enums prompted a check of how they are actually
persisted.

**The fix.** A `pg_enum()` helper pinning `values_callable`, so the value is
stored:

```python
def pg_enum[E: enum.Enum](enum_cls: type[E], name: str) -> SAEnum:
    return SAEnum(enum_cls, name=name, native_enum=True,
                  values_callable=lambda cls: [m.value for m in cls])
```

**Why it cannot recur.** A test walks every index in `Base.metadata`, parses
each predicate, and asserts every string literal exists in the corresponding
enum. The database confirms it independently: `'active'::session_status` in the
applied migration would be a hard cast error if the type held names.

**Lesson.** Silent failures are worse than loud ones, and the right fix is a
test that makes the whole class of bug impossible rather than a one-line
correction.

---

## 2. An output flood OOMs the worker, not the sandbox

**Symptom:** a submission printing in a loop grew the worker process without
bound.

**What happened.** The container had a 256 MB memory cap. Irrelevant, those
bytes are streamed *out* of the container into the supervising process. Both
`subprocess.communicate()` and docker-py's `exec_run()` buffer the entire
stream before returning, so the worker holds all of it.

The container limit protects the container. Nothing protected the supervisor.

**The fix.** Read incrementally, stop storing after 64 KB, and **keep
draining**:

```python
if total >= limit:
    flag[0] = True
    continue          # discard, but keep reading
```

The second half matters as much as the first. Stop reading and the pipe buffer
fills, the child blocks forever in `write()`, and it never exits, so the
timeout never observes it finish.

**How it was found.** Writing the test, not reading the code. The test asserted
output was bounded; watching the worker's RSS while it ran showed the real
problem.

**Lesson.** A container memory limit does not bound output, because the bytes
leave the container. The supervisor needs its own cap.

---

## 3. Docker refuses `put_archive` on a read-only container

**Symptom:** `400 Client Error: container rootfs is marked read-only`.

**What happened.** Source was staged with `container.put_archive()`. Docker
rejects that call on any container created with `read_only=True`, **even when
the destination is a writable tmpfs mount**. The check is on the container, not
the path.

**The tempting fix** was to drop `read_only`. That trades a real security
control, the one that stops a submission overwriting an interpreter and
poisoning the *next* candidate's run, for convenience.

**The actual fix.** Stream the tar into `tar -x` on stdin:

```python
exec_id = api.exec_create(cid, ["tar", "-x", "-C", "/box"], stdin=True, user="root")
sock = api.exec_start(exec_id, socket=True)
raw.sendall(archive)
raw.shutdown(socket.SHUT_WR)     # tar needs EOF or it blocks forever
```

Content never touches a command line, so there is still no `ARG_MAX` ceiling
and no shell parsing of untrusted bytes.

**Lesson.** When a library makes a security property inconvenient, find
another way to keep the property rather than relaxing it.

---

## 4. The workspace cleanup killed its own container

**Symptom:** `tar exit 1`, but only inside the sandbox, the same code worked
in an isolated reproduction.

**What happened.** Between submissions the pool clears processes a previous run
left behind:

```sh
pkill -9 -u 65534
```

The container's own PID 1 (`sleep infinity`) *also* ran as uid 65534. The
cleanup killed the container it was cleaning, and the next operation failed
against a dead container with an error that pointed nowhere near the cause.

**The fix.** The idle process runs as root; every candidate exec runs as 65534.
Not a weakening, capabilities are still fully dropped, `no-new-privileges` is
set, the rootfs is read-only, and candidate code never runs in that process.

**Lesson.** When a reproduction works and the real path fails, the difference
is in the environment, not the code.

---

## 5. A fork bomb permanently poisoned a pooled container

**Symptom:** one fork bomb, then every subsequent submission routed to that
container failed with `exec /bin/sh: resource temporarily unavailable`.

**What happened.** `pids_limit` correctly stopped the bomb. But the surviving
processes saturated the pids cgroup, and **cleanup requires a free pid to fork
`pkill` with**. There was none. Not even as root, the pid limit is
container-wide, not per-user.

A saturated container cannot be recovered from inside. It must be destroyed.

**The first fix was also wrong.** The probe was `exec true` and check the exit
code. It returned 0, because `true` is one tiny process that fits in the
single free slot the bomb leaves, while a real submission needs a shell *plus*
an interpreter. The probe passed and the container stayed poisoned.

**The working fix** measures the resource instead of sampling it:

```python
result = container.exec_run(["cat", "/sys/fs/cgroup/pids.current"], user="root")
return int(result.output) <= IDLE_PID_BUDGET       # idle sits at 2
```

Measured: idle = 2 pids, post-fork-bomb = 65 (the limit).

**Lesson.** Two things. Cleanup paths need resources too, and a health check
that samples "can one trivial thing work" is not the same as "is this resource
healthy".

---

## 6. Memory accounting was container-lifetime, not per-run

**Symptom:** every submission after a memory bomb was reported
`MEMORY_EXCEEDED`, despite succeeding and printing correct output.

**What happened.** OOM detection used
`container.attrs["State"]["OOMKilled"]`, a **container-lifetime flag**. Once
any submission OOM-ed a pooled container, the flag stayed true and every later
run inherited it. A candidate's correct answer was reported as having exhausted
memory.

**The fix.** A delta of the cgroup `oom_kill` counter, read before and after,
which is exact per run.

`memory.peak` has the same problem and **cannot be fixed the same way**:
Docker mounts `/sys/fs/cgroup` read-only inside containers, and resetting the
counter only lowers it to `memory.current`, which stays pinned by page cache
that outlives the process. So containers are retired after an OOM, and peak is
reported as a delta or as `0`, never as a plausible-looking wrong number.

**Lesson.** Pooling introduces a distinction between per-run and
per-container state, and any metric that spans runs has to be re-derived.
Reporting "not measured" is better than reporting a wrong number.

---

## 7. Reuse detection that reverted itself

**The worst bug in the project.** Not because of impact, because of what it
looked like.

**Symptom:** none visible. The logs said the mitigation had fired.

**What happened.** On refresh-token replay, the service revokes every token in
the family and raises a `401`. But the request-scoped session **rolls back on
any exception**, including the one that reports the detection.

So: the stolen token was rejected, a warning was logged saying the family had
been revoked, and the legitimate user's current token stayed valid. The account
was still fully compromised, and the logs claimed otherwise.

**A security control that reports success while doing nothing is worse than no
control**, because it stops anyone looking further.

**How it was found.** An end-to-end script that did not stop at "the replay was
rejected" but went on to check whether the *other* tokens in the family still
worked. They did.

**The fix.** One line, `await db.commit()` before raising. The security action
has to outlive the error path.

**Why it cannot recur.** An integration test against a real database rotates
twice, replays the original, and asserts *every* token in the family is dead.

**Lesson.** Verifying the happy path and the obvious rejection is not enough.
You have to assert the state the mitigation claims to have produced. Unit tests
cannot catch this one by construction: it lives entirely at the transaction
boundary, so a mocked session shows the revocation working perfectly.

---

## 8. Submissions accepted, then never evaluated

**Symptom:** `202 Accepted`, row present, worker idle, nothing ever ran.

**What happened.** Tasks used `@shared_task`, which binds lazily to
`celery.current_app`. The worker sets that from its command line, so it
resolved correctly there. The **API process never sets it**, so enqueuing fell
back to Celery's default broker, `amqp://guest@localhost`, and tried to reach
a RabbitMQ that does not exist.

The enqueue error was swallowed by the after-commit hook, which deliberately
does not propagate callback failures (the write is already durable and must not
become a 500). One log line was the only evidence.

**What made it hard.** Every component was individually healthy. The API
returned 202. The worker reported ready. Redis was up. The row existed. Only
queue depth staying at zero showed anything was wrong.

**The fix.** `@celery_app.task`, bind explicitly.

**Lesson.** Export queue depth as a metric. "Every component is up" is not the
same as "work is flowing", and only a metric that measures flow can tell the
difference.

---

## 9. The question bank was wrong, not the grader

**Symptom:** a correct hash-map Two Sum solution scored 80%, failing one hidden
case. Same in Python, C++ and JavaScript.

**What happened.** The prompt promises exactly one valid answer. The largest
test case was `target=1000000000, nums=[999999999, 1, 500000000, 500000000]`,
where `999999999 + 1` *and* `500000000 + 500000000` both hit the target. The
expected output listed only the second pair. A correct solution returning the
first was marked wrong.

**Lesson.** The grader, the sandbox and the comparison logic were all correct.
The fixture was wrong, and the result was telling a candidate their correct
answer was incorrect, which is the worst possible failure for this product.

It also demonstrates why the schema keeps per-test-case results relationally:
"which test case has the highest failure rate across all users" is a query, and
that query is how you find a badly-specified question at scale instead of by
luck.

---

## 10. Two workers, same name, old code

**Symptom:** AI reviews were never attached to submissions. The service worked
perfectly when called directly, the queue was empty, and the worker reported
itself healthy.

**What happened.** An earlier Celery worker was still running from a previous
start. Both workers had the default node name, both consumed the same queues,
and the old one still had the previous version of the review service, which was
a stub that returned `{"skipped": "not_implemented"}`.

Whichever worker picked the job up decided the outcome. Roughly half the time
it was the one running dead code.

**The only visible clue** was a line Celery prints and everyone scrolls past:

```
DuplicateNodenameWarning: Received multiple replies from node name:
celery@hostname. Please make sure you give each node a unique nodename
using the celery worker `-n` option.
```

**The fix** is that `-n` flag, so a second worker on the same host is visibly a
second worker rather than an impostor of the first.

**Lesson.** The service was never broken. The message was being delivered to a
different consumer than the one under test. Version what a worker reports about
itself, so a stale deployment is visible rather than inferred.

---

## 11. A deadlock between the test teardown and a live worker

**Symptom:** one integration test failed intermittently with
`DeadlockDetectedError`, and passed on rerun.

**What happened.** The test triggers an auto-submit, which queues submissions
for grading. Its teardown then deletes the user, cascading to their sessions
and submissions. Meanwhile a Celery worker is grading those same rows.

The two take locks in opposite orders:

- worker: lock the **submission**, then touch its **session**
- teardown: lock the **user/session**, then cascade into **submissions**

Postgres detects the cycle and kills one of them.

**The fix** was smaller than the diagnosis. The fixture that deleted sessions
was redundant, because deleting the user already cascades to them, so removing
it eliminated one of the two transactions entirely. The remaining delete retries
on `DBAPIError`, which is reasonable for a fixture that has nothing to lose.

**What it says about the system.** The lock ordering is real, not a test
artifact: hard-deleting a user while their submissions are being graded can
deadlock. Production would soft-delete instead, which is what the schema already
does for questions (`ON DELETE RESTRICT`, archive rather than remove). Sessions
and users have no such protection yet, and that is a genuine gap rather than
something the test invented.

**Lesson.** A real deadlock with a clear cause, and an uncomfortable
implication about a delete path nobody had thought about.

---

## 12. Optimising the sandbox broke it three ways at once

**Context.** Measuring the warm pool showed it was worth 1.4x, not the large
win ADR-0006 implied. The cause was ~10 Docker API round trips per execution at
roughly 60 ms each: six hundred milliseconds of bookkeeping around a program
that runs in ten. Batching them into two shell invocations was the obvious fix.

It also introduced three bugs, all caught by tests that already existed.

### 12a. The workspace stopped being wiped

The batched `prepare()` replaced a separate `reset_workspace()` call. But the
caller was still using `_acquire()` rather than `_acquire_ready()`, an earlier
edit routing it through the checked path had been clobbered by the refactor. So
`prepare()` existed, worked correctly in isolation, and was **never called**.

Submissions started seeing the previous submission's files. On a shared
question that is answer leakage.

`test_workspace_is_wiped_between_submissions` caught it. Diagnosing it took
three wrong guesses, because `prepare()` behaved perfectly every time I tested
it directly, the bug was that nothing invoked it.

### 12b. A fork bomb stopped being contained

Previously `_release()` probed the container and destroyed a saturated one. I
removed that probe as part of the batching, on the reasoning that the next
`prepare()` would catch it.

It does not, and the reason is subtle: **`prepare()` runs `pkill` first**, which
frees pids for a moment. A fork bomb still spawning passes the check, and then
saturates the container again before the next submission runs.

The fix reads `pids.current` *after* the run instead, in the counter read that
already happens, so it costs nothing, and retires any container whose
processes outlived the execution.

### 12c. A shell warning shifted a positional parse

The batched script writes `memory.peak` to reset it, which fails because Docker
mounts `/sys/fs/cgroup` read-only. `2>/dev/null` did not suppress it: the shell
opens the redirect target *before* the redirection applies, so `sh` printed
`can't create /sys/fs/cgroup/memory.peak: Read-only file system` to its own
stderr, `exec_run` merged that into stdout, and it became line one of the
output being parsed positionally.

The pid count parsed as `-1`, so every container looked unusable.

Two fixes. Brace the write so the shell's own stderr is captured
(`{ echo 0 > file; } 2>/dev/null`), and, more importantly, **keep only
numeric lines** when parsing, so the counters are identified by what they are
rather than by where they landed.

**Lesson.** This one is about optimisation rather than a mistake. The measurement was right, the fix was
right, and it still broke three things, every one of them caught by tests
written earlier for entirely different reasons. It also ends with a correction
in the docs: ADR-0006 now says 2x, with the original overstatement left visible
and marked, because a decision record that quietly edits its own numbers is
worth nothing.

---

## What these have in common

Read them together and a pattern emerges:

1. **Five of twelve were silent.** No exception and no alert, just a wrong
   number, a dead index, or a mitigation that reverted. Loud failures are the easy ones.
2. **Seven only appeared against real infrastructure.** Unit tests against the
   subprocess backend and a mocked session cannot reproduce a pids cgroup, a
   read-only mount check, or a transaction rollback.
3. **Three were fixed wrongly first.** The `exec true` probe, the
   `memory.peak` reset, and removing the release-time probe during batching
   all looked correct and were not.
4. **Optimising is where bugs come from.** The single largest cluster (#12)
   came from a change that was measured, justified and correct in its goal.
5. **The fix is usually a test, not a line.** The enum bug is one keyword
   argument; what stops it recurring is the test that walks every index
   predicate.
