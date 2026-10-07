# Project overview

Crucible is a technical interview preparation platform. Candidates practise
against timed, proctored question sets. Their code runs in a hardened sandbox
against hidden test cases, and an LLM reviews the solution and asks the
follow-up an interviewer would. A live room lets an interviewer and a candidate
share one editor in real time.

It began as a university team project in Node and MongoDB. It was rebuilt in
Python because the parts that carry the real engineering, namely running
untrusted code safely, the async grading pipeline and the AI layer, were done
naively or not at all in the original. See
[01-from-v1-to-v2](01-from-v1-to-v2.md) for the comparison and
[ADR-0001](adr/0001-rewrite-in-python.md) for the decision.

The part worth understanding first is the sandbox. Running arbitrary code from
strangers on your own hardware is structurally a remote code execution
vulnerability, and the only thing separating a code judge from one is the
quality of the confinement.

## Who uses it

| Role | What they do |
| --- | --- |
| **Candidate** | Browses questions, takes timed tests, submits solutions, reads AI feedback, joins interview rooms |
| **Interviewer** | Creates a live room, watches the candidate code, runs it, takes private notes |
| **Admin** | Authors questions and test cases, manages users |

## What it does

**Practice.** A question bank across code, SQL and multiple choice. Filter by
topic and difficulty, write a solution in the editor, run it against the sample
cases, then submit against the hidden ones.

**Timed tests.** Server-enforced deadline, autosaved answers, browser-reported
proctoring (two warnings, and the third violation ends the test), and per-topic
scoring afterwards.

**Sandboxed execution.** Python, JavaScript, C++, Java and Go, each in a
container with no network, dropped capabilities, a read-only root filesystem, a
pid limit and a memory cap.

**AI review.** Rubric-based feedback on a graded submission, plus a follow-up
question. The model never sees the answer key and never decides the score.

**Adaptive difficulty.** Elo ratings for both users and questions, updated on
every graded submission, so question difficulty corrects itself from evidence
rather than from the author's label.

**Live interview rooms.** A shared editor over WebSockets with presence, chat,
and the ability to run the shared document in the same sandbox. Every event is
logged, so a session can be replayed.

## The shape of it

```
React + Vite + Monaco
        │
        ▼
FastAPI ──── INSERT(queued) ────▶ Postgres ◀──── poll ──── queue workers
        │                                                        │
        │                                                   Docker sandbox
        ▼                                                        │
   Postgres ◀─────────────────────────────────────────────────────┘
```

One API process, stateless workers, and Postgres for both application state and
the job queue. Room fan-out is the one piece of state held only in memory, and
losing it costs a redraw rather than data. There are no microservices, which is
a deliberate choice rather than an omission; see
[02-architecture](02-architecture.md) and
[15-limitations](15-limitations.md).

## Scale of the thing

| | |
| --- | --- |
| Backend | ~9,500 lines of Python |
| Frontend | ~2,000 lines of TypeScript |
| Tests | 161 (102 unit, 59 integration), ~2,300 lines |
| Database | 14 tables, 43 indexes, 11 native enum types |
| Documented decisions | 11 ADRs |
| Bugs written up | 12 |

## Where to go next

- The engineering is in [06-sandbox-deep-dive](06-sandbox-deep-dive.md) and
  [07-async-evaluation](07-async-evaluation.md).
- The component boundaries are in [02-architecture](02-architecture.md).
- The honest assessment of what is missing is in
  [15-limitations](15-limitations.md).
