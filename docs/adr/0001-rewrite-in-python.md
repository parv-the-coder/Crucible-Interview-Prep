# ADR-0001: Rewrite in Python rather than incrementally refactoring the Node service

Status: Accepted
Date: 2026-08-23

## Context

v1 is a working Express + MongoDB application, roughly 4,000 lines, built as a
university software-architecture project. It has real merit: a Strategy pattern
for evaluation, a BullMQ job queue, idempotency keys, a dead-letter queue,
health probes and worker telemetry.

It also has structural problems that are not fixable by editing a few files:

1. The sandbox applies three controls (`--network none`, `--cpus`, `--memory`)
   and is missing the ones that matter most, no pid limit, writable root
   filesystem, full Linux capabilities, running as uid 0.
2. Caches are per-process `Map` objects, so the service cannot be run as more
   than one instance without users seeing inconsistent data.
3. The data is thoroughly relational and is stored in a document database, so
   joins are done in application code.
4. Telemetry is hand-rolled counters that nothing can scrape or alert on.

The intended direction of the project (LLM-based review, embeddings and
semantic retrieval, adaptive scheduling) is work the Python ecosystem serves
far better than the Node one.

So the question is not "Python or JavaScript". It is: **port, or refactor in
place?**

## Options considered

### Option A: Incrementally harden the existing Node service

Fix the sandbox, swap the caches to Redis, add Prometheus, keep Mongo.

**Pros.** No rewrite risk. The existing tests keep working. Every commit ships
something. This is the correct answer for a system with users.

**Cons.** The database choice is the expensive mistake, and it is the one thing
an incremental path never gets around to fixing. The AI work would mean either
calling Python services from Node or using less mature client libraries. And
the finished product is a hardened version of a quiz runner, not a
noticeably more capable system.

### Option B: Rewrite in Python, port the domain model, discard the code

**Pros.** The relational model gets fixed at the point where fixing it is
cheapest. Async I/O throughout. The AI and scheduling work sits in the
ecosystem built for it. The result is a system worth talking about rather than
a tidied-up version of one.

**Cons.** Rewrites are the classic engineering mistake, a well-documented way
to spend six months arriving where you started. Everything working in v1 has to
be rebuilt and re-proven. No commit ships value until quite late.

### Option C: Strangler fig: new Python service alongside, migrate route by route

**Pros.** The textbook answer for a live system. Risk is spread over time.

**Cons.** Requires running two stacks, two datastores and a routing layer, for
a system whose entire user base is a project team. The overhead is real and the
risk it manages is not.

## Decision

**Option B.** Rewrite.

The deciding factor is that this system has **no users and no uptime
obligation**. The single strongest argument against a rewrite, that you must
keep the existing thing alive while rebuilding it, does not apply here. What
remains is a straightforward comparison of end states, and the Python end state
is better on the dimensions that matter for where this project is going.

The domain model carries over almost unchanged. That is the part that took real
thought in v1 (question types, timed sessions, proctoring, submission
lifecycle) and none of it is language-specific. What gets discarded is
plumbing.

## Consequences

### What this makes easy
- Fixing the data model at the only point where it is cheap.
- Async request handling with genuinely mature libraries.
- The AI layer, embeddings and scheduling maths.
- Setting a higher quality bar from line one: typed models, real test coverage, linting in CI.

### What this makes hard
- Nothing is demonstrable until several layers exist at once.
- Every v1 behaviour must be deliberately re-derived. Some are load-bearing and non-obvious; the risk is quietly dropping one.
- No incremental delivery. The project is unusable for a while.

### What we will have to revisit
- If this ever gets real users, the next significant change must be incremental. This is a one-time licence justified by having nothing to break, and it does not renew.

## In short

The reason for rewriting rather than refactoring is not that Python is better:

> Rewrites are usually the wrong call, and the reason is that you have to keep
> the old system alive while you rebuild. That constraint did not exist here,
> no users, no uptime obligation. The expensive mistake in v1 was using MongoDB
> for data that is thoroughly relational, and an incremental path never gets
> around to fixing the datastore. Since the one real argument against rewriting
> did not apply, I compared the end states instead. If this had live users I
> would have hardened the sandbox in place and left the database alone.
