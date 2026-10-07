# ADR-0006: Warm container pool, with bounded reuse

Status: Accepted
Date: 2026-08-23

## Context

Creating a container costs roughly 300 to 800 ms depending on image and host. A
code question with 20 test cases that creates a container per case pays that
20 times, 6 to 16 seconds of pure overhead against a 10-second budget, before
any candidate code runs.

v1 did exactly this: `docker run` per test case.

## Options considered

### Option A: One fresh container per test case
**Pros.** Perfect isolation between cases. Simplest possible reasoning.
**Cons.** Overhead dominates total time. Unusable for multi-case questions.

### Option B: One fresh container per *submission*, reused across its cases
**Pros.** Cuts N startups to 1. Still perfectly isolated between users, since a
container never outlives one submission.
**Cons.** Still pays full startup on every submission.

### Option C: Warm pool: long-lived containers, `exec` per run
**Pros.** Acquire drops from ~500 ms to ~1 ms on a pool hit.

> **Corrected after measuring.** Acquire is indeed ~1 ms, but acquire was never
> the cost. End-to-end the win is **2x**, not the ~500x that number implies:
> 825 ms unpooled versus 411 ms pooled. The remaining time is Docker exec
> round trips and the program itself. Measuring also revealed ~600 ms of
> bookkeeping overhead per execution that has since been batched away. See
> [11-performance.md](../11-performance.md) §4.
**Cons.** A container now serves multiple submissions from *different users*.
That is a real isolation regression and must be handled deliberately.

## Decision

**Option C, with explicit mitigations**, because the isolation regression is
the whole point of the decision and pretending otherwise would be the mistake.

Containers idle on `sleep infinity` and each run is an `exec`. Between runs:

1. **Kill stray processes.** `pkill -9 -u 65534`, a previous submission may have
   backgrounded something. Without this it would keep running while the next
   candidate's code executes alongside it.
2. **Wipe the workspace.** The tmpfs is emptied. Otherwise submission N+1 could
   read submission N's source, which for a shared question is answer leakage.
3. **Bounded reuse.** After `sandbox_pool_max_reuses` (default 50), the container
   is destroyed rather than recycled. This caps how far any residue we failed to
   clean could ever propagate.
4. **Fail closed.** If the reset itself fails, the container is retired
   immediately instead of being returned to the pool.

The residual risk is that the reset is incomplete in a way not anticipated,
for example, a kernel-level side channel between execs. Bounded reuse limits
the blast radius; it does not eliminate it. For a system grading strangers'
code against a shared answer key, this is an accepted risk. For one running
genuinely adversarial code, Option B is the right trade and the pool should be
disabled (`SANDBOX_POOL_ENABLED=false`), which is why it is a config flag and
not a hard-coded behaviour.

## Consequences

### What this makes easy
- Multi-case questions finish in a usable time.
- Far higher throughput per worker host.
- `sandbox_pool_acquire_seconds{hit="true|false"}` makes the benefit measurable rather than asserted -- which is how the 500x claim above was caught.

### What this makes hard
- Reset correctness is now security-critical code.
- A leaked pool on worker crash accumulates containers, which is why `reap_orphans()` and `worker_shutting_down` cleanup exist.

### What we will have to revisit
- If the platform is ever opened to genuinely adversarial users, revert to per-submission containers. The flag exists for exactly that.

## In short

> Container startup is 300 to 800 ms and a 20-test-case question pays it 20 times,
> so I pool warm containers and exec into them. Measured end to end it is worth
> about 2x, 16.5 s down to 8.2 s for a 20-case question. I originally wrote
> that acquire drops from 500 ms to 1 ms, which is true and misleading: acquire
> was not the cost, and measuring it properly turned up 600 ms per run of my
> own telemetry calls that I have since batched into two. The catch is that a container now serves multiple users, which
> is a real isolation regression. So between runs I kill stray processes, wipe
> the tmpfs workspace, and cap reuse at 50 before destroying the container, so
> anything I failed to clean can't travel far. If the reset fails, the
> container is retired instead of reused. If I were running genuinely hostile
> code I'd turn pooling off and take the latency, it's a config flag for
> exactly that reason.
