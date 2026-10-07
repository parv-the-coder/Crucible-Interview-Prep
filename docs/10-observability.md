# Observability

Two signals, each answering a different question:

| Signal | Answers |
| --- | --- |
| **Logs** | What happened to *this* request? |
| **Health** | Can this process serve traffic right now? |

The test of whether observability is real is not whether data exists, v1 had
counters, but whether you can **correlate across processes**. v1 could not.

## Logs

structlog, JSON, with `request_id` on every line, propagated through
`contextvars` so async tasks inherit it without manual threading. `user_id` and
`session_id` join once known.

```json
{"event":"submission.accepted","request_id":"9f2c1a…","user_id":"a48e…",
 "submission_id":"af35…","type":"code","timestamp":"2026-08-24T09:12:03Z"}
```

The point is incident forensics. One `grep` on a request id returns the whole
causal chain across two processes: the API accepting the submission, the worker
claiming it, the sandbox result, and the AI review. The id is echoed in the
`X-Request-ID` response header, so a user can paste it into a bug report and it
resolves to exactly what happened.

An inbound `X-Request-ID` is honoured, so a trace spans the edge as well.

**Events are named, not formatted.** `submission.accepted` with structured
fields, never `f"Submission {id} accepted"`. You can aggregate on the former.

**Routes are logged by template, never by path.** `/submissions/{id}`, not
`/submissions/af35…`. Logging the concrete path makes access logs impossible to
group, because every submission UUID reads as a distinct endpoint.

## Health

Two endpoints, because they mean different things to an orchestrator:

- **`/health/live`**: is the process running? Touches nothing. Conflating this
  with readiness makes an orchestrator restart healthy pods during a database
  blip.
- **`/health/ready`**: can it serve? Checks Postgres and the sandbox in
  parallel, returns 503 if not.

Readiness also reports **which sandbox backend is live and whether it is
production-safe**:

```json
"sandbox": { "ok": true, "backend": "docker", "production_safe": true, "warnings": [] }
```

A deployment that fell back to the insecure executor cannot look healthy. That
line is a security control expressed as a health check.

## Detecting a stalled queue

Health checks answer "is every component up", which is **not** the same
question as "is work flowing". There is a bug in this project that makes the
distinction concrete: submissions were accepted and never evaluated, and every
component reported healthy: the API returned 202, the worker said ready, the
broker was up, and the row existed. ([14-bugs-found](14-bugs-found.md) #8.)

Flow is therefore checked in the database rather than inferred from uptime.
`reap_stuck_submissions` runs on a timer inside the worker and scans for rows left in
`running` past a generous cutoff, backed by the `ix_submissions_inflight`
partial index so the scan stays proportional to the number of stuck rows rather
than the whole table. It requeues what it can, fails what has exhausted its
retry budget, and, the part that matters here, emits

```json
{"event":"submissions.reaped","requeued":3,"abandoned":0,"level":"warning"}
```

That line only ever appears when work has stopped flowing, which makes it the
thing to alert on.

## What I would alert on

Ranked, because an alert that does not lead to action is noise:

1. **`submissions.reaped` appearing repeatedly**, workers are dying or
   capacity is short, and both need a human.
2. **`sandbox.internal_error` rising**, the sandbox is broken, not the
   candidate's code.
3. **`/health/ready` failing**, stop routing traffic.
4. **`http.unhandled` rate above baseline**, unhandled 5xx.
5. **`auth.refresh_reuse_detected` appearing at all**, one is a client bug, a
   pattern is credential theft.
6. **`ai.call_failed` sustained**, feature degraded, not an outage, so lower
   priority.

Note what is *not* on the list: CPU and memory. They are symptoms. Whether
submissions are draining is the thing that actually tells you if the system is
working.

## What this deliberately does not have

No metrics backend, no tracing, no dashboards. At this scale structured logs
plus health checks answer nearly every question, and each of those components is
another service to run, secure and keep alive. The honest tradeoff: questions of
the form "what is the p95 right now" need a log aggregator to answer, and
"where exactly did the time go inside this request" cannot be answered at all.
Both become worth their operational cost at a traffic level this project does
not have.
