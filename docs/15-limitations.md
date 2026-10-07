# Limitations and known gaps

This system is well built but it is not production hardened, and this document
is the inventory of the difference: what is genuinely limited, what a production
team would do differently, what was simplified on purpose, and what to fix
first.

---

## Real limitations

### 1. A kernel exploit escapes the sandbox

Containers are namespaces, cgroups and capabilities, all enforced by a kernel
shared with the host. Dirty COW, Dirty Pipe and several io_uring bugs all
escaped containers.

This is the honest limit of shared-kernel isolation, and no amount of Docker
configuration closes it. For genuinely untrusted multi-tenant load you would
want gVisor or Firecracker. The sandbox sits behind an interface
(`evaluation/sandbox/base.py`) specifically so that swapping the backend is a
one-function change, to be made when the threat model justifies the syscall
overhead.

### 2. Pooled containers serve more than one user

This is a real isolation regression, taken deliberately in exchange for a much
faster acquire path. It is bounded by process kills between runs, a workspace
wipe, a cap on reuses, a fail-closed reset and a read-only root filesystem. The
residual risk is a contamination channel nobody anticipated. Set
`SANDBOX_POOL_ENABLED=false` for genuinely adversarial users. See
[ADR-0006](adr/0006-warm-container-pool.md).

### 3. Access tokens stay valid for up to 15 minutes after a ban

Stateless JWTs are not checked against the database, so banning a user does not
invalidate the access token they already hold. Closing this means a database
lookup per request, which defeats the point of stateless auth. A blocklist table
consulted only on sensitive endpoints would be the middle ground if it mattered.

### 4. A single Postgres primary

No read replicas and no partitioning, while `submissions` grows without bound.
At tens of millions of rows the table should be partitioned monthly on
`created_at`, and the schema is already compatible with that. Read replicas for
analytics come before it. Neither is justified at current scale.

### 5. The warm pool is worth about 2x, not the 500x first claimed

ADR-0006 originally justified pooling with "acquire drops from about 500 ms to
about 1 ms". Acquire does, but acquire was never the dominant cost. Measured end
to end the win is about 2x. The same measurement exposed roughly 600 ms per
execution of telemetry round trips, since batched away. The original claim is
left visible in the ADR rather than quietly edited.

### 6. LLM output is non-deterministic and can be wrong

The AI reviewer can produce incorrect feedback, which is why AI output is never
in the scoring path. Test cases are ground truth and the model only explains the
result. If the provider is down, feedback is `null` and the grade still stands.
Every call is written to `ai_interactions`, so a disputed piece of feedback is
reproducible.

### 7. The subprocess sandbox provides no real isolation

It exists so the platform runs without Docker and so the unit suite needs no
daemon. It is loud about it: `production_safe` is false, readiness surfaces it,
and constructing it outside local or test raises. Importantly, Docker being
unavailable in production does **not** silently fall back to it. Turning "Docker
is down" into "we are running untrusted code unconfined" is a much worse failure
than an outage.

---

## What a production team would do differently

| Gap | What production would want | Why it is not here |
| --- | --- | --- |
| No per-user rate limiting on the sandbox | Token bucket per user on submission creation | Designed, not enforced end to end |
| No CI pipeline | Lint, typecheck and both suites on every push | Commands are documented in [12-testing](12-testing.md); nothing runs them automatically |
| Hard-deleting a user can deadlock | Soft delete, as questions already use | Found via a test; the lock ordering is real |
| No WAF or DDoS protection | Cloudflare or equivalent at the edge | Infrastructure concern, outside application scope |
| Secrets in `.env` | Vault, AWS Secrets Manager or SOPS | Fine locally, would not ship |
| No audit log for admin actions | Append-only admin action log | Proctoring violations are audited; admin actions are not |
| No backup or restore runbook | Tested point-in-time recovery, documented RTO and RPO | Not written |
| No blue/green or canary deploy | Progressive rollout with automatic rollback | Single-environment project |
| Docker socket access from workers | Rootless Docker, or a dedicated executor service | Socket access is roughly host root |
| No PII handling policy | Data export and deletion | Emails are stored; no deletion flow exists |

---

## Deliberate simplifications

These are choices, not oversights.

- **SQLite for SQL grading, not Postgres.** A real engine with a real query
  planner, free to create and discard per submission. Dialect differences exist
  and are documented for question authors.
- **Elo, not item response theory.** IRT needs hundreds of responses per item.
  Elo works from the first one. See
  [ADR-0010](adr/0010-elo-for-adaptive-difficulty.md).
- **An operation log, not a CRDT.** CRDTs solve offline peer-to-peer
  convergence. There is a central authority and a transactional database here,
  so a unique constraint on `(room_id, version)` does the job. See
  [ADR-0009](adr/0009-operation-log-for-rooms.md).
- **No microservices.** A modular monolith is the right architecture for one
  team and this traffic. Splitting it would add network calls, distributed
  transactions and deployment complexity to buy independent scaling nothing
  needs.
- **Proctoring is client-reported.** Tab-blur and fullscreen-exit events come
  from the browser, and a determined cheat can suppress them. It raises the cost
  of casual cheating; it does not stop a motivated attacker. Real proctoring
  needs a lockdown browser or video, both out of scope.

---

## What to fix first, in order

1. **Rate limiting on submission creation.** The sandbox is the most expensive
   resource in the system and it is currently unmetered per user. This is the
   nearest real hole.
2. **CI.** The suite exists and nothing runs it automatically, which means it
   rots the first week someone forgets.
3. **Load testing to establish real SLOs.** The numbers in
   [11-performance](11-performance.md) are real but were measured on one laptop,
   sequentially. There is no honest concurrent-user figure.
4. **gVisor for the sandbox**, if the user base ever became genuinely untrusted.
5. **Partitioning `submissions`**, once it approaches tens of millions of rows.
6. **A secrets manager**, before anything resembling production.
