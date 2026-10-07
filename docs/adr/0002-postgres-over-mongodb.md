# ADR-0002: PostgreSQL instead of MongoDB

Status: Accepted
Date: 2026-08-23
Amended: 2026-09-01, the `pgvector` element of this decision was dropped;
see *Amendment* below. The Postgres-over-Mongo decision itself is unchanged.

## Context

v1 used MongoDB with Mongoose. The data it stores is:

```
User ──< TestSession ──< SessionItem >── Question ──< TestCase
  │                          │                          │
  └──────< Submission >──────┘                          │
                 └──< SubmissionResult >────────────────┘
```

Every arrow is a foreign key. Almost every read crosses at least one.

Concrete queries the product needs:

- "This user's submissions, newest first, with question title and difficulty."
- "Per-topic pass rate for this user over the last 30 days."
- "Which test case fails most often across all users?" (finds badly-worded questions)
- "Leaderboard: users ranked by distinct questions solved this month."
- "Session summary: every item, its question, its final submission, its score."

In v1 these were `Promise.all` of several `find()` calls stitched together in
JavaScript, or `$lookup` aggregations that are joins with worse ergonomics.

## Options considered

### Option A: Keep MongoDB (with Beanie or Motor)

**Pros.** Zero migration. Flexible schema suits per-type question payloads.
Horizontal sharding if it were ever needed.

**Cons.** Joins in application code. N+1 queries, or manual batching. No
foreign keys, so nothing prevents a submission pointing at a deleted question.
No CHECK constraints, so `pass_count > attempt_count` is possible. Multi-document
transactions exist but require a replica set and are not the common path.
Window functions (needed for ranking and streaks) do not exist.

### Option B: PostgreSQL

**Pros.** Joins done by a query planner rather than by me. Real referential
integrity and CHECK constraints. Partial and expression indexes. Window
functions for ranking, running totals and streaks. JSONB for the parts that
genuinely are schemaless.

**Cons.** Schema changes need migrations. Less obvious horizontal scaling.
Per-question-type payloads need either JSONB or a table-per-type.

### Option C: Postgres for relational data, Mongo retained for question documents

**Pros.** Each store does what it is best at.

**Cons.** Two datastores, two backup strategies, two failure modes, no
cross-store transactions, and a synchronisation problem, for a system whose
entire dataset fits in memory. Operational cost with no matching benefit.

## Decision

**Option B.** PostgreSQL 16, SQLAlchemy 2.0, Alembic for migrations.

The per-type payload problem is solved with a hybrid: everything relational
(users, sessions, submissions, test cases, results) is properly normalised, and
the genuinely type-varying part (MCQ choices, SQL fixtures, grading rubrics)
lives in a JSONB `payload` column, validated at the API boundary by a
type-specific Pydantic validator.

This is the important nuance. The choice was never "rigid schema vs flexible
schema". Postgres gives both: constraints where the shape is known, JSONB where
it is not. Mongo only offers the second.

## Consequences

### What this makes easy
- One query instead of four round trips plus application-side stitching.
- The database refuses to store contradictory data (`pass_count <= attempt_count` is enforced, not hoped for).
- Analytics that would be painful in Mongo: percentiles, ranks, streaks.

### What this makes hard
- Every schema change is a reviewed, reversible migration. Slower, and correct.
- Scaling writes past one primary would need partitioning or sharding. Not a real constraint at this scale.

### What we will have to revisit
- `submissions` is the table that grows without bound. At tens of millions of rows it wants monthly partitioning on `created_at`. The schema is already compatible with that; it is deferred, not designed out.

## Amendment (2026-09-01)

`pgvector` was part of the original decision but was never implemented, no
vector column ever reached the schema, and question search is plain Postgres
full-text (`to_tsvector`). The dependency was removed rather than left as an
unused import promising a capability the system does not have. Nothing else in
this ADR changes: the argument for Postgres never rested on vectors.

If semantic search is wanted later, the case for `pgvector` over a separate
vector store is the same one made above, one store, one transaction, no sync
job, and adding it is an extension migration plus a column.

## In short

> The data is relational, users, sessions, questions, submissions and per-case
> results are all foreign keys, and v1 was doing those joins in JavaScript.
> Postgres also gives me CHECK constraints and partial indexes, which is how I
> stopped the database from being able to hold contradictory data at all. The
> flexible part, the per-question-type payload, is JSONB validated at the API
> boundary, so I get schema where the shape is known and flexibility where it
> isn't. Mongo only offers the second half of that.
