# ADR-0004: Celery instead of ARQ, Dramatiq or RQ

Status: Superseded by [ADR-0011](0011-postgres-queue-over-celery.md)
Date: 2026-08-23
Superseded: 2026-09-01

> This decision stood while the requirements below held. It was reversed when
> the "observable / scrapeable" requirement was dropped along with Prometheus,
> and the multi-queue requirement turned out not to be load-bearing at this
> scale. The reasoning that follows is preserved as written; ADR-0011 records
> what changed and why.

## Context

Submissions are evaluated asynchronously. The API accepts a submission,
persists it and returns `202 Accepted` in single-digit milliseconds; a worker
does the ten-second sandbox run. This decouples a slow, spiky, untrusted
workload from the request path.

Requirements:
- At-least-once delivery with acknowledgement after work completes, not on receipt.
- Retries with backoff, and a bounded retry budget.
- Multiple queues, so a slow LLM call cannot sit in front of a candidate's code run.
- Scheduled jobs (session expiry, stuck-job reaping).
- Observable: queue depth and task outcomes must be scrapeable.

## Options considered

### Option A: ARQ
Asyncio-native, Redis-backed, small and readable, by the author of pydantic.
**Pros.** Fits FastAPI's async model exactly. One engine would suffice (ADR-0003
would disappear). Simple enough to read end to end in an afternoon.
**Cons.** Smaller ecosystem, no equivalent of Flower, fewer people have operated
it. And the actual workload is blocking container supervision, so async buys
little here.

### Option B: Dramatiq
**Pros.** Cleaner API than Celery, good middleware model, sane defaults.
**Cons.** Smaller community. Scheduling requires a separate package.

### Option C: RQ
**Pros.** The simplest of all.
**Cons.** No native scheduling or task routing. Would need building by hand.

### Option D: Celery
**Pros.** The mature default: routing, `beat` scheduling, rate limits, retries,
Flower, and every failure mode already documented by someone who hit it in
production. Prefork suits blocking sandbox work, a hung container takes down
one child process, not an event loop shared with every other task.
**Cons.** Large, old, configuration-heavy. Unsafe defaults (`acks_late=False`,
`prefetch_multiplier=4`) that silently lose work if left alone. Not async, which
forces ADR-0003.

## Decision

**Option D.** Celery, with the defaults explicitly overridden:

| Setting | Default | Ours | Why |
| --- | --- | --- | --- |
| `task_acks_late` | `False` | `True` | Default acks on *receipt*, a worker killed mid-evaluation loses the submission silently |
| `task_reject_on_worker_lost` | `False` | `True` | Requeue rather than drop when a worker dies |
| `worker_prefetch_multiplier` | `4` | `1` | Prefetched jobs starve idle workers and are lost on crash |
| `accept_content` | `pickle` allowed | `json` only | Pickle deserialisation is RCE for anyone who can write to the broker |
| `worker_max_tasks_per_child` | unlimited | `200` | Bounds any slow fd/memory leak from long-lived Docker clients |

The honest reason to prefer Celery over ARQ is *not* that it is technically
better for this workload. ARQ arguably is. It is that Celery's failure modes
are documented by thousands of people who hit them in production, and that
matters more than elegance for the part of the system that must not lose a
candidate's work.

## Consequences

### What this makes easy
- Queue separation (`evaluation`, `ai`, `maintenance`) is one config line.
- `beat` gives scheduled sweeps without another component.
- Flower for queue introspection during a demo.

### What this makes hard
- Two database engines (ADR-0003).
- Celery's defaults are actively dangerous, so the configuration must be understood rather than copied.

### What we will have to revisit
- If the workload shifts to being dominated by LLM calls (I/O-bound, high concurrency, cheap per task) rather than sandbox runs, prefork stops being the right pool and ARQ becomes the better fit.

## In short

> Celery, but the defaults are wrong for this. Out of the box it acknowledges a
> job on receipt, so a worker killed mid-evaluation loses the submission
> silently. I set acks_late and reject_on_worker_lost. And prefetch defaults
> to 4, which means one worker sits on jobs its idle peers could be running,
> and loses all of them if it dies. Set to 1. I'd also say ARQ is arguably a
> better technical fit since it's async-native, but Celery's failure modes are
> well documented by people who hit them in production, and for the component
> that must not lose someone's work I'd rather have boring and known.
