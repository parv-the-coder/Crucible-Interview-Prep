# ADR-0011: A Postgres-backed queue instead of Celery and Redis

Status: Accepted
Date: 2026-09-01
Supersedes: [ADR-0004](0004-celery-over-arq.md)

## Context

ADR-0004 chose Celery on Redis, and the reasoning there was sound for the
requirements it listed. Two of those requirements no longer hold:

- **"Observable: queue depth and task outcomes must be scrapeable."** The
  Prometheus/Grafana stack was removed; nothing scrapes anything now. Queue
  depth is answered by a `SELECT count(*)`.
- **"Multiple queues, so a slow LLM call cannot sit in front of a candidate's
  code run."** Real, but it was solving a problem this deployment does not
  have. One worker process grades a submission and then reviews it; capacity
  comes from running more workers, not from partitioning one worker's attention.

What remains is a system running two datastores where one would do, and paying
for it in a specific way: **a submission was written to Postgres and enqueued to
Redis as two separate operations.** The code went to real lengths to manage that
gap, an after-commit hook so the job was never enqueued before the row was
visible, and a reaper to catch the opposite case where the row committed but the
enqueue never happened. That is a correctness problem created entirely by having
the queue live somewhere other than the data.

## Decision

The `submissions` table **is** the queue. A row in `QUEUED` is a pending job.

A worker claims one with

```sql
SELECT id FROM submissions
WHERE status = 'queued'
ORDER BY enqueued_at
FOR UPDATE SKIP LOCKED
LIMIT 1
```

followed by the conditional `UPDATE ... WHERE status = 'queued'` that already
existed. `SKIP LOCKED` is what makes this a queue rather than a bottleneck:
without it, every idle worker blocks on the same row and throughput collapses
to one worker's worth, however many are running.

Periodic sweeps run on a timer inside the worker, guarded by
`pg_try_advisory_lock` so that N workers still produce one sweep.

## Consequences

### What this makes easy
- **The enqueue race cannot exist.** `INSERT` and "enqueue" are the same write
  in the same transaction, so they cannot disagree. The after-commit hook and
  its careful ordering comment are deleted, not reimplemented.
- One datastore, one backup, one failure mode. Local setup drops a container.
- The queue is inspectable with SQL. "What is waiting, and since when" is a
  query, not a broker introspection tool.
- Retries need no separate counter: `attempt` is incremented by the claim, so
  it bounds retries by construction.

### What this makes hard
- **Rooms are single-process now.** Redis pub/sub carried room events between
  API processes; without it, two API processes cannot see each other's room
  traffic. Rooms need sticky routing or one process. Postgres `LISTEN/NOTIFY`
  would restore this without bringing Redis back, and is the intended path.
- **Polling adds latency.** A submission waits up to `POLL_INTERVAL_SECONDS`
  (1s) before pickup, where a broker pushes. Against a sandbox run measured in
  seconds this is small, and it is the price of not being able to miss work the
  way a dropped notification can.
- **No backoff between retries.** Celery's `countdown` is gone; a failing
  submission retries as fast as it is re-claimed. Bounded by
  `submission_max_attempts`, so the worst case is three fast retries, but a
  transiently broken sandbox burns a submission's budget faster than it should.
  A `not_before` column would fix it if that becomes a real problem.
- **Polling costs a query per second per idle worker.** It is one indexed count
  against a partial index; at this scale it does not register, but it is not
  free the way a blocking pop is.

### What we will have to revisit
- At high job rates the single-row `SELECT ... LIMIT 1` becomes contended. The
  fix is claiming in batches, which the same query supports with a larger
  `LIMIT`.
- If work ever needs fan-out to many consumers, or priorities that a single
  `ORDER BY` cannot express, a real broker earns its place again.

## In short

> I had Celery on Redis, and it worked, but it meant the submission row and the
> job that graded it lived in different systems, so the code had an
> after-commit hook to stop the worker seeing a job before the row committed,
> and a reaper for the reverse case where the row committed and the enqueue was
> lost. That whole class of bug exists only because the queue wasn't in the
> database. Postgres has `FOR UPDATE SKIP LOCKED`, which is a queue: the row is
> the job, so enqueueing is just the INSERT and the two can't disagree. I gave
> up push latency and multi-process WebSocket fan-out for it. At one worker and
> a sandbox run measured in seconds, that trade is clearly right; at a thousand
> jobs a second it isn't, and I'd go back to a broker.
