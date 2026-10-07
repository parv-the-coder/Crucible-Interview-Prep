# ADR-0009: Versioned operation log for collaborative editing, not a CRDT

Status: Accepted
Date: 2026-08-23

## Context

The live interview room shares one code editor between an interviewer and a
candidate. Two people typing in the same document at the same time is the
classic distributed-systems problem: without conflict resolution, concurrent
edits produce divergent documents.

Realistic constraints for *this* system, which differ from Google Docs:

- 2 to 4 participants, not hundreds.
- Everyone is connected to the same backend, with a database behind it.
- Sessions last under an hour.
- Offline editing is not a requirement, if you disconnect, you rejoin.

## Options considered

### Option A: Last-write-wins on the whole document
**Pros.** Trivial.
**Cons.** Two people typing means one silently loses a keystroke's worth of
work, or more. Visibly broken within seconds of real use.

### Option B: Operational Transformation (what Google Docs used)
**Pros.** Proven, character-level, minimal data on the wire.
**Cons.** Transformation functions are notoriously subtle, the literature is
full of published OT algorithms later shown to be incorrect. Needs a central
server (which we have) but implementing it correctly is a project in itself.

### Option C: CRDT (Yjs, Automerge)
**Pros.** Correct by construction. Handles offline and peer-to-peer. Excellent
libraries exist.
**Cons.** Metadata overhead per character; documents accumulate tombstones. Needs
a client-side library and a compatible server. Solves the *hard* version of the
problem, offline, decentralised, unbounded peers, none of which we have.

### Option D: Append-only operation log with optimistic concurrency
Each edit is an operation tagged with the document version it was based on.
The server accepts an operation only if its version is current; otherwise the
client receives the operations it missed, rebases and retries.

**Pros.** The concurrency control is a single unique constraint,
`UNIQUE (room_id, version)`, so correctness is enforced by Postgres, not by an
algorithm I wrote. The log is durable, replayable and auditable, which also
gives session playback for free. Simple enough to reason about completely.
**Cons.** A rejected operation costs a round trip. Under genuinely heavy
concurrent typing that becomes noticeable. Not suitable for many concurrent
editors.

## Decision

**Option D.**

```
UNIQUE (room_id, version)
```

Two clients racing to write version N: Postgres lets exactly one INSERT
succeed. The loser gets a conflict, fetches the winner's operation, rebases its
own edit and retries. Fan-out to other sockets is *transport only*: the log in
Postgres is the source of truth, so losing fan-out cannot corrupt a document.

> **Amended 2026-09-01.** Fan-out was Redis pub/sub, which carried operations
> between API processes. With Redis removed it is process-local, so rooms now
> need sticky routing to one API process. This ADR's decision, the operation
> log as the source of truth, is what makes that a capacity limit rather than
> a correctness one, and is unchanged. See [ADR-0011](0011-postgres-queue-over-celery.md).

Periodic snapshots (`document`, `snapshot_at_version`) mean a late joiner loads
a snapshot plus a short tail rather than replaying thousands of operations.

The reasoning that decided it: **CRDTs solve a harder problem than we have.**
They exist for offline editing and peer-to-peer convergence without a central
authority. We have a central authority with a transactional database. Using a
unique constraint for concurrency control is not a compromise here, it is the
right tool, and it is a tool whose correctness I do not have to prove.

## Consequences

### What this makes easy
- Correctness rests on a database constraint, not on an algorithm's proof.
- Session recording and playback come free from the log.
- Debugging: the exact sequence of edits is queryable.

### What this makes hard
- Conflict retries cost a round trip; with 2 to 4 users this is imperceptible, with 20 it would not be.
- The client must implement rebase logic.

### What we will have to revisit
- If rooms ever need many concurrent editors or offline support, this is where Yjs becomes the correct answer. It is a bounded rewrite of one module.

## In short

> An append-only operation log with a unique constraint on (room_id, version).
> Two clients racing for the same version means one INSERT fails, and that
> client rebases and retries, so Postgres is doing the concurrency control,
> not code I wrote. I deliberately didn't use a CRDT: CRDTs solve offline,
> peer-to-peer convergence with no central authority, and I have a central
> authority with a transactional database. For 2 to 4 participants who are all
> online, a unique constraint is the right tool and I don't have to prove its
> correctness. If I needed offline editing or twenty concurrent users I'd
> switch to Yjs, and it's isolated enough to be a bounded change.
