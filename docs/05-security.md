# Security

The sandbox has its own document, [06-sandbox-deep-dive](06-sandbox-deep-dive.md),
because it is the largest surface by a wide margin. This covers everything
else.

## Threat model

| Actor | Wants | Can do |
| --- | --- | --- |
| **Candidate** | The answer key; a better score | Authenticate, submit arbitrary code, call any endpoint their role allows |
| **Stranger** | An account; the question bank | Anything unauthenticated |
| **Compromised account** | Escalate to admin | Everything a candidate can, with valid tokens |
| **Interviewer** | none | Sees candidate code; that is the feature |

The dominant one is the first. A candidate is *invited* to run arbitrary code on
our hardware, so the interesting attacks are the legitimate feature used
aggressively.

## Authentication

15-minute access tokens, 14-day rotating refresh tokens.
[ADR-0007](adr/0007-jwt-with-rotating-refresh.md) has the full reasoning.

**Argon2id, not bcrypt.** bcrypt silently truncates input at 72 bytes, so two
long passwords sharing a 72-character prefix verify against each other's hash.
It is also memory-light, which is the property GPU cracking rigs exploit.
Parameters target 50 to 100 ms per hash.

**Rotation with reuse detection.** Every refresh mints a new token and revokes
the presented one, so a refresh token is single-use and replay is *detectable*.
On replay we cannot tell whether the attacker or the legitimate user is
replaying, so the entire family is revoked and both must sign in again. A stolen
token is worth one use plus an alert instead of fourteen days.

> This was cosmetic for a while. The revocation was rolled back by the
> `HTTPException` that reported it, so the logs claimed a mitigation that had
> not happened, worse than no detection, because it stops anyone looking
> further. [14-bugs-found](14-bugs-found.md) #7.

**Algorithm pinned at decode.** `algorithms=["HS256"]` explicitly, never read
from the token header, that is the `alg=none` and HS/RS confusion class.

**Token type is a claim and is checked.** A refresh token presented as a bearer
token is rejected; otherwise stealing one yields a 14-day access credential.

**Issuer is checked**, so a token from another service signed with a shared
secret does not authenticate here.

**Refresh tokens stored as SHA-256, not Argon2.** The input is 300+ bits of our
own entropy, so there is no dictionary to attack and the lookup must be fast.
Using Argon2 here would be cargo-culting.

## Enumeration

Sign-in returns one message for both "no such user" and "wrong password", and
burns equivalent CPU via `dummy_verify()` when the account does not exist.
Without that, "no such user" returns in microseconds while a wrong password
takes ~80 ms, and **response latency enumerates valid email addresses**. A test
asserts the two paths stay within the same order of magnitude.

Sign-up is generic for the same reason: "email already registered" is an
enumeration oracle too.

Resource access follows the same principle, 404 rather than 403 for someone
else's submission, session or room.

## Authorisation

Role gates are a dependency factory with admin encoded once:

```python
async def _guard(user: CurrentUser) -> User:
    if user.role is UserRole.ADMIN or user.role in roles:
        return user
    raise HTTPException(403, ...)
```

Encoding "admin passes everything" in one place means no endpoint can forget
it, which is how privilege checks drift apart.

Room membership is checked **before** the WebSocket is accepted, so a
non-participant never holds an open socket to a room they are not in.

## The answer key

The single most valuable thing a candidate could steal, and the defences are
structural rather than procedural:

- `TestCaseOut` **cannot represent** a hidden case. There is no shape to leak into.
- `to_detail()` is the only path from a `Question` to a response model.
- Payload filtering is an **allow-list**, so a new field is hidden by default.
- Hidden-case stdout is never persisted as visible.
- Rubric *weights* are withheld while criterion names are shown, names help, weights let you game the grader.
- The AI reviewer never receives hidden cases or the reference solution.

## Input handling

**SQL injection** is not reachable: every query goes through SQLAlchemy with
bound parameters. The one place raw SQL exists is the SQL *grading* strategy,
which runs candidate SQL against a throwaway in-memory SQLite database, behind
two independent gates, a regex rejecting mutating statements, and SQLite's own
authorizer callback denying anything that is not a read.

**Command injection** is not reachable in the sandbox: argv is always a list,
never a shell string. Where a shell is unavoidable (stdin redirection), argv is
passed through positional parameters so nothing user-controlled is parsed.

**Payload sizes** are bounded at the schema, source code 200 KB, chat 4 KB,
idempotency keys 64 bytes (that one becomes part of a unique index, and an
unbounded header would blow the btree row limit).

**Pickle is disabled on the broker.** `accept_content=["json"]`. Pickle
deserialisation is arbitrary code execution for anyone who can write to the
broker. The queue no longer has a serialisation boundary at all -- a worker
reads a row -- so this class of attack has nowhere to land.

## Secrets

`SecretStr` everywhere, so an accidental `repr` cannot leak them. Configuration
refuses to boot outside local/test with a JWT secret under 32 bytes (RFC 7518
§3.2) or still set to the development default. PyJWT only warns, which is far
too easy to miss in a log.

**Secrets are in a `.env` file**, which is fine locally and would not ship. A
real deployment needs a secrets manager. This is listed in
[15-limitations](15-limitations.md) rather than pretended away.

## Fail-closed decisions

Two places where the safe direction was chosen explicitly:

**Docker unavailable in production does not fall back** to the subprocess
sandbox. Turning "Docker is down" into "we are now running untrusted code
unconfined" is a far worse failure than an outage.

**The insecure backend refuses to construct** outside local/test, reports
`production_safe: false` on the readiness endpoint, and logs a warning at
startup. A deployment running it cannot look healthy.

## Known gaps

Stated plainly, because the alternative is being surprised by them:

- **No rate limiting on submission creation.** The sandbox is the most
  expensive resource in the system and is currently unmetered per user. This is
  the nearest real hole and the first thing I would fix.
- **The worker's Docker socket access is equivalent to root on the host.** In
  production the worker should be the only thing that can reach it, ideally via
  rootless Docker or a dedicated executor service.
- **Access tokens remain valid up to 15 minutes after a ban.** The cost of
  stateless auth; closing it means a lookup per request.
- **The room WebSocket token travels in a query string**, because the browser
  WebSocket API cannot set headers. Query strings land in proxy logs. Mitigated
  by short token lifetime; a single-use ticket endpoint is the proper fix.
- **Proctoring is client-reported** and defeatable by anyone who opens the
  console. It raises the cost of casual cheating; it does not stop a motivated
  attempt.
- **No secrets manager, no WAF, no admin audit log.**

## What is tested

Security controls without tests are aspirations. The suite executes the
attacks: `alg=none`, a forged signature, a foreign issuer, a refresh token used
as an access token, refresh replay revoking a family, timing equalisation, fork
bombs, memory bombs, output floods, filesystem reads, network access, and
container writes.
