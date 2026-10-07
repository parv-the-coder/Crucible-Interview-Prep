# Crucible engineering documentation

These documents explain not just what was built, but why, what was rejected,
and what each trade-off cost. Nothing here assumes you already know the
codebase.

Start with [00-project-overview](00-project-overview.md) if you are new, or go
straight to the area you need.

## Contents

| # | Document | What it covers |
| --- | --- | --- |
| 00 | [Project overview](00-project-overview.md) | What the platform does, who uses it, and the shape of the system |
| 01 | [From v1 to v2](01-from-v1-to-v2.md) | The Node and MongoDB original, what was wrong with it, and what the rewrite changed |
| 02 | [Architecture](02-architecture.md) | Components, request flows, where state lives, failure behaviour, and how to scale it |
| 03 | [Data model](03-data-model.md) | Every table, every index, and the query each one serves |
| 04 | [API design](04-api-design.md) | Endpoints, the error contract, pagination, and versioning |
| 05 | [Security](05-security.md) | Threat model, authentication, authorisation, and the gaps |
| 06 | [Sandbox deep dive](06-sandbox-deep-dive.md) | How untrusted code is confined: every control and the attack it blocks |
| 07 | [Async evaluation](07-async-evaluation.md) | The queue, idempotency, crash recovery, and retry policy |
| 10 | [Observability](10-observability.md) | Structured logs, health checks, and what is worth alerting on |
| 11 | [Performance](11-performance.md) | Measured numbers, where the time goes, and a claim that was corrected |
| 12 | [Testing](12-testing.md) | Test strategy, the suite layout, and what is deliberately not tested |
| 13 | [Deployment](13-deployment.md) | Requirements, first run, configuration, and troubleshooting |
| 14 | [Bugs found](14-bugs-found.md) | Twelve real defects, each with root cause, fix, and the change that stops it recurring |
| 15 | [Limitations](15-limitations.md) | What is genuinely limited, what production would need, and what to fix first |

## Decisions

[`adr/`](adr/) holds the Architecture Decision Records: one per significant
choice, in a fixed format of context, options considered, decision, and
consequences. Each one names the alternatives that were rejected and why, so
"PostgreSQL over MongoDB" comes with the reasoning and the accepted cost rather
than just the conclusion.

## Where the engineering is

If you only read three, read these:

- [06-sandbox-deep-dive](06-sandbox-deep-dive.md), because running untrusted
  code safely is the hardest problem in the system.
- [07-async-evaluation](07-async-evaluation.md), for the queue semantics and
  what happens when a worker dies mid-run.
- [14-bugs-found](14-bugs-found.md), for the defects that shaped the design,
  including five that failed completely silently.

## A note on honesty

Some parts of this system are genuinely good, some are adequate, and a few are
deliberately simplified in ways a production team would do differently. This
documentation says which is which, and
[15-limitations](15-limitations.md) is the summary of the gap.
