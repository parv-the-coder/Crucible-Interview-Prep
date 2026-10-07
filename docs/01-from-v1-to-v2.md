# From v1 to v2

v1 was a team project for a university software-architecture course: Express,
MongoDB, BullMQ, React. It worked and it got a good grade.

This document is a review of that codebase, written going back to it as a
reviewer rather than as one of its authors. It records what the rewrite was
responding to, specifically enough to name the line rather than the vibe.

## What v1 got right

Worth saying first, because a critique that finds nothing good is not a
critique.

- **Strategy pattern for evaluation.** Code, MCQ and SQL each had their own
  strategy behind a factory. That is the right shape and v2 keeps it.
- **Idempotency keys on submissions.** Genuinely thoughtful for a student
  project. Most people do not think about a retried POST at all.
- **A dead-letter queue.** Failed jobs went somewhere instead of vanishing.
- **Health endpoints with dependency checks**, including worker heartbeat.
- **Structured-ish logging** with request IDs.

The bones were sound. What follows is about the parts underneath.

## The sandbox

```javascript
// v1: Task4/backend/src/evaluation/dockerRunner.js
const args = [
  "run", "--rm",
  "--network", "none",
  "--cpus", "1",
  "--memory", "256m",
  "-e", `CODE_B64=${codeB64}`,
  profile.image, "sh", "-c", profile.command
];
```

Three controls. The ones that were missing mattered more than the ones present:

| Missing | Consequence |
| --- | --- |
| `--pids-limit` | **A fork bomb takes down the host.** `while true; do :& done`, a first-week-of-Unix prank |
| `--read-only` | Code can overwrite an interpreter in the image |
| `--cap-drop ALL` | `CAP_SYS_ADMIN` retained: the starting point of every container-escape write-up |
| `--user` | Runs as root inside the container |
| `--security-opt no-new-privileges` | setuid binaries in the base image can escalate |
| `--memory-swap` | Docker defaults it to *double* `--memory`, so the 256 MB cap silently allowed 512 |
| Output cap | Output accumulated in a Node string; a print loop OOMs the worker |

The fork bomb is the one to lead with. It needs no exotic knowledge and it is a
live denial of service against the whole machine.

**Also:** source was base64-encoded into an `sh -c` string. That breaks on
`ARG_MAX` for a large submission, and it puts a shell in the path of
attacker-controlled input for no reason.

## The data model

The data is thoroughly relational:

```
User ──< TestSession ──< SessionItem >── Question ──< TestCase
  │                          │                          │
  └──────< Submission >──────┘                          │
                 └──< SubmissionResult >────────────────┘
```

Every arrow is a foreign key, and it was in a document database. So joins
happened in JavaScript, `Promise.all` of several `find()` calls stitched
together in application code.

The knock-on effects matter more than the joins:

- **No foreign keys**, so a submission could point at a deleted question.
- **No CHECK constraints**, so `pass_count > attempt_count` was representable.
- **No partial indexes**, so "find submissions stuck in RUNNING" scans everything.
- **No window functions**, so ranking and streaks are application code.

## Caching

```javascript
const summaryCache = new Map();
```

In-process. Correct for exactly one instance, and silently wrong the moment
there are two: two users hit two processes and see different data, and an
invalidation on one does nothing to the other. It puts a ceiling on the
architecture that is invisible until you try to scale past it.

## Telemetry

Hand-rolled counters exposed on an admin endpoint. Nothing could scrape them,
so nothing could alert on them. The information existed and could not be used.

## What v2 changed, and why

| Area | v1 | v2 | Why |
| --- | --- | --- | --- |
| Sandbox | 3 controls | Defence in depth, tested against real attacks | A fork bomb was a host DoS |
| Database | MongoDB | PostgreSQL | The data is relational; constraints make bad states unrepresentable |
| Cache | in-process `Map` | none; Postgres is the only store | A second store is a second thing to invalidate wrongly |
| Queue | BullMQ | Postgres `FOR UPDATE SKIP LOCKED` | The row is the job, so the INSERT and the enqueue cannot disagree |
| Telemetry | custom counters | structlog, JSON, request-id correlated | One grep returns a whole request across processes |
| Difficulty | static labels | Elo on users and questions | Author labels are frequently wrong |
| Results | poll until done | WebSocket in rooms, polling for submissions | Polling a 500 ms operation is mostly "not yet" |
| AI | none | Rubric review, hints, follow-ups | The actual differentiator |
| Live interviews | none | Shared editor with an operation log | The other differentiator |

## Would a rewrite have been the right call with users?

No, and it is worth saying so plainly, because the honest answer is more
convincing than the confident one.

Rewrites are usually wrong, and the specific reason is that you must keep the
old system alive while rebuilding it. That constraint did not exist here, no
users, no uptime obligation. With the single strongest argument against
rewriting removed, the decision reduces to comparing end states.

With real users I would have hardened the sandbox in place, that is a
contained change with an enormous payoff, and left MongoDB alone until
something else forced the issue.

Full reasoning: [ADR-0001](adr/0001-rewrite-in-python.md).
