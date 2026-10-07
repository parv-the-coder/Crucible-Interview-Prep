# Performance

Every number here was measured on this project, not estimated. Where a
measurement contradicted something I had written down, the write-up was
corrected rather than the number.

**Test machine.** 8-core laptop, 16 GB RAM, Docker 29 with overlayfs and cgroup
v2, Postgres 16 in a container, a single uvicorn process and three queue
workers on the host. This is a development machine, not a server: treat
these as relative comparisons rather than capacity planning.

---

## 1. Read latency

Forty requests each, warm.

| Endpoint | p50 | p95 |
| --- | --- | --- |
| `GET /questions?limit=50` | 14.6 ms | 17.7 ms |
| `GET /questions/{id}` | 16.2 ms | 18.5 ms |
| `GET /auth/me` | 10.0 ms | 12.5 ms |
| `GET /submissions?limit=20` | 15.7 ms | 17.1 ms |

`/auth/me` is the floor, and it is the interesting one: it decodes a JWT and
loads one row by primary key. Roughly 10 ms is therefore the fixed cost of a
request here: Python, the ORM, and a database round trip over a container
network. The listing endpoints add only 4 to 6 ms on top, which says the queries
and their indexes are not the bottleneck at this size.

Nothing here is optimised, and nothing here needs to be.

## 2. Submission acknowledgement

The number that matters, because a candidate is watching.

| | p50 | p95 |
| --- | --- | --- |
| `POST /submissions` | 24.8 ms | 32.1 ms |

That covers validating the payload, checking the question, inserting the row,
committing, and queueing the job. The sandbox run is not in it, that is the
entire point of the async design. Grading the same submission synchronously
would have made this endpoint take **700 ms to 16 s** depending on the number
of test cases.

## 3. Grading throughput

25 submissions of a 5-test-case Python question, 3 workers, cold pool:

| | |
| --- | --- |
| Wall clock | 37.6 s |
| Per submission | ~1.5 s |
| Sandbox execution p50 | 721 ms |
| Queue wait p50 | 16.7 s |
| Queue wait p95 | 29.5 s |

The queue wait is the honest headline. Submitting 25 jobs at once to 3 workers
means the last one waits half a minute. That is not a defect, it is what a
queue does under a burst, and it is why the API returns immediately instead of
holding a connection. It is also exactly the signal that would drive
autoscaling: **queue depth, not CPU.**

Throughput here is `workers ÷ per-submission time` ≈ 2 submissions/second. To
serve more, add workers; they are stateless and coordinate only through
Postgres.

## 4. The warm pool, and a claim I got wrong

ADR-0006 justified pooling containers. When I finally measured it, the
justification was overstated and the implementation was worse than it should
have been.

**First measurement, before any tuning:**

| | p50 |
| --- | --- |
| Fresh container per run | 1057 ms |
| Pooled container | 758 ms |
| **Speedup** | **1.4x** |

I had written "acquire drops from ~500 ms to ~1 ms", implying something near a
500x win. Acquire *is* ~1 ms on a pool hit. But acquire is not the cost,
**the surrounding bookkeeping was.**

Counting Docker API calls per execution:

```
prepare:   pkill + rm  (1)   reset peak (1)   read peak (1)   probe (1)
run:       stage files (1)   oom before (1)   run (1)
after:     read peak (1)     oom after (1)
                                            ~10 round trips
```

Each `docker exec` is a round trip over the daemon socket at roughly 60 ms.
**Six hundred milliseconds of asking the daemon questions, to supervise a
program that runs in ten.** The pooling saved container creation and then spent
the savings on telemetry.

**The fix** was to batch: one shell invocation that cleans the workspace and
prints the pid count, memory peak and OOM counter together, and one more after
the run. Ten round trips became four.

| | p50 | p95 |
| --- | --- | --- |
| Fresh container per run | 825 ms | 865 ms |
| Pooled container | **411 ms** | 424 ms |
| **Speedup** | **2.0x** | |

Pooled execution went from 758 ms to 411 ms, a 1.8x improvement on the pooled
path alone. Both paths got faster, because both were paying the same tax.

For a 20-test-case question: **16.5 s unpooled versus 8.2 s pooled.**

**What this cost to learn.** Batching those calls broke three things at once,
all found by the existing tests: the workspace stopped being wiped between
submissions (the caller was still using the un-prepared acquire path), a
fork-bombed container stopped being retired, and the shell's own "read-only
file system" warning shifted a positional parse so every container looked
unusable. Details in [14-bugs-found.md](14-bugs-found.md).

**The honest summary:** pooling is worth 2x, not 500x. The
number I originally wrote measured the wrong thing, the acquire, not the
operation. Measuring it turned up 600 ms of self-inflicted overhead I would
never have found by reading the code.

## 5. Where the time actually goes

A pooled 411 ms execution, roughly:

| Stage | Approx. |
| --- | --- |
| `prepare` (clean + counters, 1 exec) | ~60 ms |
| Stage source via `tar` on stdin (1 exec) | ~60 ms |
| Run the program (1 exec) | ~230 ms |
| Read counters (1 exec) | ~60 ms |

Even now, **roughly 40% is Docker API overhead rather than the candidate's
code**. That is the real cost of using containers as the isolation boundary,
and it is the number that would justify moving to a persistent runner protocol
or a different sandbox technology, not a vague sense that containers are slow.

## 6. What I have not measured

Being explicit, because an unmeasured claim is not a result.

- **No load test.** There is no k6 or Locust run, so there is no honest
  concurrent-user figure. Everything above is sequential or a small burst.
- **No query plans.** Indexes were designed from the access patterns rather
  than from `EXPLAIN ANALYZE` output. At ten questions and a few hundred rows
  the planner would sequential-scan everything regardless, so measuring now
  would prove nothing.
- **No production numbers.** One machine, one process, containerised
  datastores on the same host. Real network latency, real disk and real
  contention are absent.

If asked "what would you do next", this is the ranked answer: load test to find
which resource actually saturates first, because my guess is sandbox capacity
and a guess is not a measurement.
