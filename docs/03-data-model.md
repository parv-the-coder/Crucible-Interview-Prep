# Data model

14 tables, 43 indexes, 11 native enum types. The design rule throughout: **make
bad states unrepresentable in the database**, not merely unlikely in the
application.

## The shape

```
users ──┬──< refresh_tokens          (rotation families)
        ├──< topic_mastery           (per-topic Elo)
        ├──< test_sessions ──< session_items >── questions ──< test_cases
        │         └──< violations                    │              │
        ├──< submissions >───────────────────────────┘              │
        │         └──< submission_results >──────────────────────────┘
        ├──< interview_rooms ──< room_participants
        │         └──< room_events   (append-only op log)
        └──< ai_interactions          (audit + cost ledger)
```

## Choices that apply everywhere

**UUID primary keys.** IDs can be minted client-side, which makes idempotent
retries straightforward, and sequential integers leak business information,
submission #48,213 tells you exactly how much activity the platform has had.
The cost is index locality: random UUIDs scatter B-tree inserts. It does not
matter at this scale, and UUIDv7 would recover most of it if it did.

**`timestamptz` everywhere, defaulted server-side.** Clock authority stays with
Postgres. A client-supplied timestamp is a client-controlled timestamp.

**Native enums, storing values not names.** This is the subtle one:
SQLAlchemy persists the member *name* by default, so `SessionStatus.ACTIVE`
becomes `'ACTIVE'`, and a partial index predicated on `status = 'active'`
matches nothing, forever, silently. `pg_enum()` pins `values_callable`, and a
test walks every index predicate asserting each literal exists in its enum.
See [14-bugs-found](14-bugs-found.md) #1.

**Explicit constraint naming convention.** Without it Alembic autogenerates
anonymous constraints a downgrade cannot drop, which makes migrations one-way
in practice.

**Every foreign key declares `ON DELETE`.** A test enforces it. Leaving deletion
semantics to chance is how you get orphans or surprise cascades.

## The tables that carry the design

### `submissions`

The busiest table and the one with the most deliberate structure.

| Column | Why it exists |
| --- | --- |
| `status` | Moves forward only. The transition to `running` is a conditional UPDATE, which is what makes duplicate job delivery harmless |
| `idempotency_key` | Sparse unique with `user_id`. A retried POST returns the original instead of paying for a second sandbox run |
| `started_at`, `attempt` | Exist for crash recovery, not display. The reaper finds rows stuck in `running` past a deadline and bounds retries |
| `is_dry_run` | A "Run" is not graded and does not affect ratings |
| `queue_wait_ms`, `execution_ms` | Separated deliberately: one is a capacity problem, the other is a code problem |

Four indexes, each serving a named query:

```sql
-- "my submissions, newest first"
ix_submissions_user_recent (user_id, created_at)

-- the stuck-job reaper. Partial, so the scan is O(stuck) not O(all)
ix_submissions_inflight (started_at) WHERE status IN ('queued','running')

-- leaderboards and rating updates, excluding practice runs
ix_submissions_graded (user_id, question_id, created_at)
  WHERE status = 'completed' AND NOT is_dry_run
```

The partial indexes are the interesting ones. `submissions` grows without bound
but the set of *in-flight* rows is tiny, so the reaper's index stays small
forever regardless of history size.

### `test_cases`, relational, not a JSON blob

The obvious modelling is a JSON array on `questions`. Keeping it as a table
means `submission_results` can foreign-key to a specific case, which turns

> "which test case fails most often across all users?"

into a query. That query is how you find a question whose wording is wrong,
and it is exactly the bug that shipped in the seed data
([#9](14-bugs-found.md)), where the grader was right and the fixture was wrong.

### `session_items`, drafts live here, not on submissions

The editor autosaves continuously. If each save created a submission, a
30-minute test would produce thousands of graded attempts. Drafts live on the
session item; only submit turns them into submissions. A browser crash then
loses nothing.

### `room_events`, the constraint *is* the algorithm

```sql
UNIQUE (room_id, version)
```

Two clients racing to write version N: Postgres lets exactly one INSERT
succeed. The loser is sent what it missed, rebases and retries. There are no
transform functions to prove correct, and no CRDT metadata to carry. See
[ADR-0009](adr/0009-operation-log-for-rooms.md).

### `ai_interactions`, a ledger, not a log

Three reasons it is a table rather than a log line:

1. Per-user daily budgets are counted from it, so the number cannot drift from
   what actually happened.
2. An AI-authored piece of feedback must be reproducible if it is disputed.
3. Stored prompt/response pairs are the dataset for evaluating a prompt change.

```sql
-- "how many billable calls has this user made today"
ix_ai_interactions_budget (user_id, created_at) WHERE NOT cached AND ok
```

Cached and failed calls are excluded because neither costs anything.

### `violations`, append-only on purpose

If a result is disputed, the evidence must not be something the application can
quietly rewrite. There is no update path.

## Where JSONB is used, and where it is not

Relational for everything with a known shape. JSONB for exactly three things:

- `questions.payload`: genuinely per-type (MCQ choices, SQL fixtures, rubrics), validated at the API boundary by a type-specific Pydantic validator
- `submissions.answer`: non-code answers
- `room_events.payload` / `ai_interactions.response`, opaque event bodies

This is the actual answer to "why not MongoDB": the choice was never rigid
schema versus flexible schema. Postgres gives both, constraints where the
shape is known, JSONB where it is not. Mongo only offers the second half.

## Constraints that prevent real bugs

```sql
CHECK (pass_count <= attempt_count)          -- questions
CHECK (cases_passed <= cases_total)          -- submissions
CHECK (score >= 0 AND score <= max_score)    -- submissions, session_items
CHECK (ends_at > starts_at)                  -- test_sessions
CHECK (duration_seconds BETWEEN 60 AND 21600)
CHECK (document_version >= snapshot_at_version)  -- interview_rooms
```

Each of these is a bug that cannot happen rather than a bug that has not
happened yet. They cost nothing on write and they mean a corrupt row is caught
at its source instead of being discovered later in a report.

## What I would change at scale

- **Partition `submissions` by month.** It is the only table that grows without
  bound. The schema is already compatible; doing it now would be premature.
- **Archive `room_events`** for ended rooms to cold storage.
- **Read replicas** for analytics, so a slow aggregate cannot affect grading.

None of these are needed at current size, and saying so is part of the answer.
