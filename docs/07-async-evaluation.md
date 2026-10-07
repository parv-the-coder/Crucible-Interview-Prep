# Asynchronous evaluation

How a submission gets from an HTTP request to a graded result, and every place
that can go wrong.

---

## 1. Why not just evaluate in the request?

A code submission takes up to ten seconds to grade, compile, then run once per
test case, each in a container. Doing that inside the HTTP handler means:

- A connection held open for ten seconds, occupying a worker the whole time.
- A client timeout or a refresh loses the result entirely.
- API capacity and sandbox capacity become the same resource, so a burst of
  submissions makes *logging in* slow.
- No retry. If the container daemon hiccups, the candidate's work is gone.

So the API does the small part, validate, persist, enqueue, return `202`, and
a worker does the slow part.

```
POST /submissions
  │
  ├─ validate (question exists, language allowed, non-empty)
  ├─ INSERT submission (status=queued)   ◀── this *is* the enqueue
  ├─ COMMIT
  └─ 202 { id, poll_url }
                   measured at ~50 ms

worker (N of them, no coordination)
  ├─ SELECT ... WHERE status='queued' FOR UPDATE SKIP LOCKED LIMIT 1
  ├─ claim (conditional UPDATE -> running)
  ├─ load question + test cases
  ├─ run sandbox per case
  └─ persist results
```

## 2. The ordering problem: enqueue before or after commit?

This is the first real decision, and both orders are wrong in different ways.

**Enqueue inside the transaction.** The worker is fast. It can dequeue the job
and `SELECT` the submission *before the INSERT is visible to other
connections*. It finds nothing, concludes the submission does not exist, and
either fails or, worse, silently drops it. This is a genuine race, not a
theoretical one: the window is the duration of the commit, and workers poll
continuously.

**Enqueue after commit.** Now the row always exists when the job is picked up.
The new failure is the process dying between the commit and the enqueue: a
submission committed as `queued` that nobody was ever told about.

We choose **after commit**, because the two failures are not equally bad:

- Enqueued-but-invisible → *lost work*, no trace, candidate sees nothing.
- Committed-but-not-enqueued → *recoverable*, because a row sitting in `queued`
  is exactly what the stuck-submission reaper looks for.

Always fail in the direction you can recover from.

```python
# crucible/services/submissions.py -- the whole enqueue, now
db.add(submission)          # status=QUEUED
# ...and that is it. Commit publishes it; there is no second system to tell.
```

The after-commit hook this section described has been **deleted**. It existed to
order two writes to two systems; with one system there is nothing to order. That
is the clearest measure of what the change bought: the safest handling of a
problem is not having it.

> **This design hid a real bug, and it is why the broker is gone.** Tasks were
> registered with `@shared_task`, which binds lazily to `celery.current_app`.
> The worker set that from its command line; the API process never did, so it
> fell back to Celery's default broker, `amqp://guest@localhost`, and tried
> to reach a RabbitMQ that does not exist. Submissions were written and never
> enqueued. Every component looked healthy: 202 from the API, "ready" from the
> worker, Redis up, the row present. The only signal was that nothing drained.
>
> The narrow lesson was "bind tasks explicitly". The real one is that the bug
> was only *possible* because the row and the job lived in different systems.
> A queue in the same database as the data cannot be misconfigured to point at
> the wrong broker, because there is no broker to point at.
> ([ADR-0011](adr/0011-postgres-queue-over-celery.md).)

## 3. Exactly-once, and why you cannot have it

Interviewers ask for exactly-once delivery. The correct answer is that it does
not exist across an unreliable network, it is equivalent to the Two Generals
problem. What you can build is **at-least-once delivery plus an idempotent
consumer**, which is observationally equivalent and is what everyone means.

The queue gives at-least-once: the reaper requeues a row whose worker died, so
the same submission can legitimately be handed out twice. The idempotency is one
statement:

```python
claimed = db.execute(
    update(Submission)
    .where(Submission.id == sid, Submission.status == SubmissionStatus.QUEUED)
    .values(status=SubmissionStatus.RUNNING, started_at=now(), worker_id=... attempt=Submission.attempt + 1)
    .returning(Submission.id)
).scalar_one_or_none()

if claimed is None:
    return {"skipped": True}      # someone else has it, or it is already done
```

Postgres serialises concurrent updates to the same row, so **exactly one caller
sees a rowcount of 1**. Everything else exits without running the code.

The naive version is the bug:

```python
submission = db.get(Submission, sid)          # both workers read QUEUED
if submission.status == "queued":             # both pass the check
    submission.status = "running"             # both proceed
```

Two workers, two containers, two sets of results for one submission. The
conditional UPDATE closes the window because the check and the write are the
same atomic operation.

## 4. The broker's failure modes, and why there is no broker

This section used to document the Celery settings that had to be overridden to
stop the queue losing work, late acknowledgement, prefetch of 1, refusing
pickle. Every one of those was managing the gap between *the row* and *the job*.

There is no gap now. The row is the job:

| Broker failure | Why it cannot happen here |
| --- | --- |
| Acknowledged on receipt, then the worker dies → work lost | A claim is an `UPDATE` to `running`. A dead worker leaves it there, and the reaper requeues it. Nothing is ever "done" because a message was delivered. |
| Committed row, failed enqueue → row stuck in `queued` forever | `INSERT` and enqueue are one transaction. They cannot disagree. |
| Prefetch: one worker hoards jobs its idle peers could run, and loses them all if it dies | A worker holds exactly one claim at a time; `SKIP LOCKED` hands the next row to whoever asks. |
| Pickle deserialisation = RCE for anyone who can write to the broker | There is no serialisation boundary. The worker reads a row. |

The tradeoff bought with that: **polling latency**. A worker notices new work
within `POLL_INTERVAL_SECONDS` rather than instantly. Against a sandbox run
measured in seconds it does not matter, and unlike a pushed notification a poll
cannot be missed.

The old reasoning is preserved in [ADR-0004](adr/0004-celery-over-arq.md), which
[ADR-0011](adr/0011-postgres-queue-over-celery.md) supersedes.

<!-- retained: the point below is about at-least-once generally, not Celery -->
Late acknowledgement is what makes at-least-once real. Without it you have
at-most-once, and the idempotent claim protects against a failure that can no
longer happen, while the failure that *does* happen (silent loss) is
unprotected.

## 5. Evaluation happens outside the transaction

```python
with sync_session_scope() as db:
    claim(...)                    # short transaction
    ctx = build_context(...)      # snapshot everything the strategy needs

result = strategy.evaluate(ctx)   # ← up to 10 seconds, NO transaction open

with sync_session_scope() as db:
    persist(result)               # short transaction
```

Holding a transaction across sandbox execution would pin a database connection
for ten seconds per submission, at concurrency 4, that is 4 connections doing
nothing but waiting, and hold row locks *while running untrusted code*.

This is why `EvaluationContext` is a frozen dataclass rather than ORM objects.
A strategy that cannot reach a `Session` cannot lazily load an attribute across
a closed connection, cannot accidentally commit, and cannot leave the caller's
transaction in a surprising state.

## 6. When a worker dies

Three layers, because each covers a failure the previous one cannot.

**Clean loss**, worker receives SIGTERM, finishes or rejects.
`acks_late` + `reject_on_worker_lost` returns the job to the queue.

**Unclean loss**. OOM killer, host disappears, SIGKILL. The broker never
learns. The submission sits in `RUNNING` forever. This is what the reaper is
for:

```python
def reap_stuck_submissions():
    cutoff = now() - (sandbox_timeout * 10 + 180)
    for submission in submissions_running_since_before(cutoff):
        if submission.attempt >= MAX_ATTEMPTS:
            fail(submission, "exceeded retry budget after worker loss")
        else:
            requeue(submission)
```

This is why `Submission` carries `started_at` and `attempt`, they exist to
make crash recovery possible, not for display.

**Poison pill**, a submission that reliably kills workers. `attempt` bounds
retries; after three it is marked `FAILED`. Without that bound, one bad
submission cycles forever and is a self-inflicted denial of service.

## 7. Retries are decided, not automatic

`autoretry_for=()` is set deliberately. Blanket auto-retry retries *everything*,
including errors that will never succeed:

```python
except UnsupportedQuestionTypeError:
    fail_permanently(...)          # retrying cannot help
except Exception as exc:
    if self.request.retries < MAX:
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)
    fail_permanently(...)
```

Exponential backoff, because the usual cause of a transient failure is
something overloaded, and retrying immediately makes it worse.

Note the claim is *released* before a retry, status goes back to `QUEUED`, so
the retry can re-acquire it. Retrying while still holding `RUNNING` would fail
its own claim check and skip.

## 8. Results are replaced, not appended

```python
db.query(SubmissionResult).filter(submission_id == sid).delete()
for case in result.cases:
    db.add(SubmissionResult(...))
```

A retried submission must not accumulate two generations of results. Appending
would leave a submission showing "8/5 cases passed", which is both wrong and
alarming.

## 9. Queue separation

Three queues, because one slow consumer must not block a fast one:

- `evaluation`: sandbox runs. Latency-sensitive; a candidate is waiting.
- `ai`: LLM calls. Seconds to tens of seconds, and externally rate-limited.
- `maintenance`: periodic sweeps.

Without separation, a burst of AI reviews sits in front of a candidate's code
run, and a 30-second LLM call adds 30 seconds to someone's test.

## 10. What the candidate sees

`202` returns `poll_url` and `websocket_url`. Polling works and is the
fallback; the WebSocket is the intended path, because polling a submission that
takes 500 ms means most requests return "still running" and the ones that
matter are late by up to a poll interval.

## 11. Questions you should be able to answer

**"How do you guarantee exactly-once processing?"**
> You can't, it's impossible across an unreliable network. What you build is
> at-least-once delivery plus an idempotent consumer. Mine is a conditional
> UPDATE on `WHERE status = 'queued'`; Postgres serialises row updates so
> exactly one worker gets rowcount 1 and the duplicates exit.

**"What if the worker crashes halfway through?"**
> The row stays in RUNNING, because a claim is a status change rather than a
> message acknowledgement, there is no "already delivered, therefore done"
> state to be stranded in. A reaper finds submissions stuck in RUNNING past a
> deadline and puts them back to QUEUED, which *is* requeueing them, bounded by
> an attempt counter so a poison submission can't cycle forever.

**"Why not enqueue inside the transaction?"**
> That question stops applying once the queue is a table: the INSERT is the
> enqueue, so it is inside the transaction by construction and there is no
> ordering to get wrong. With a broker it mattered a great deal, enqueue too
> early and the worker finds no row; too late and a crash loses the work, and
> the code carried an after-commit hook to manage exactly that.

**"Why a Postgres queue over Celery?"**
> I had Celery, and it worked. But the submission and the job that graded it
> lived in different systems, which produced a real outage: the API silently
> enqueued to a broker that wasn't there, and every health check stayed green.
> `SELECT ... FOR UPDATE SKIP LOCKED` makes the row the job, so that class of
> bug is gone. I gave up push latency, backoff between retries, and
> multi-process WebSocket fan-out. At one worker and ten-second sandbox runs
> that's clearly the right trade; at a thousand jobs a second it isn't.
