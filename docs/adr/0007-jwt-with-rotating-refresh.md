# ADR-0007: Stateless access tokens + rotating refresh tokens

Status: Accepted
Date: 2026-08-23

## Context

v1 issued a single JWT with a 24-hour lifetime and no revocation path. If that
token leaked, the attacker had a full day of access and there was nothing the
system could do about it.

The tension is fundamental: a stateless token is fast because nobody checks a
database, and unrevocable *for exactly the same reason*.

## Options considered

### Option A: Server-side sessions (opaque token, Redis lookup per request)
**Pros.** Instantly revocable. Nothing sensitive in the token.
**Cons.** A Redis round trip on every request; Redis becomes a hard dependency
of authentication. Horizontal scaling means Redis is now a shared bottleneck.

### Option B: Long-lived JWT (v1's approach)
**Pros.** No lookup at all.
**Cons.** Unrevocable. A leaked token is valid until it expires. Logout is a
client-side lie.

### Option C: Short access token + long refresh token, no rotation
**Pros.** Leaked access token expires in minutes. Refresh is checkable.
**Cons.** A stolen *refresh* token is valid for its full lifetime and its use is
indistinguishable from legitimate use.

### Option D: Short access token + rotating refresh token with reuse detection
**Pros.** All of C, plus theft becomes *detectable*.
**Cons.** More moving parts. Requires storing token state. A network failure
mid-rotation can log a legitimate user out.

## Decision

**Option D.**

- **Access token:** JWT, HS256, 15 minutes, never checked against the database.
- **Refresh token:** JWT, 14 days, SHA-256 hash stored in `refresh_tokens`, grouped by `family_id`.

The mechanism that makes theft detectable:

1. Every refresh mints a new token and **revokes the presented one**. A refresh token is therefore single-use.
2. If an already-revoked token is presented, one of two things happened: an attacker stole it and the real user has since rotated, or the attacker rotated first and the real user is now replaying.
3. **We cannot tell which.** So we revoke the entire family and force a fresh sign-in.

That reduces a stolen refresh token's value from "14 days of access" to "one
use, then both parties are locked out and the theft is visible in the logs".
This is the standard OAuth 2.0 BCP recommendation for public clients.

Supporting choices:

- **Tokens stored as SHA-256, not Argon2.** The input is 300+ bits of our own entropy, so there is no dictionary to attack and the lookup must be fast. Argon2 here would be cargo-culting.
- **Algorithm pinned at decode.** `algorithms=["HS256"]` explicitly, never read from the token header, that is the `alg=none` and HS/RS confusion class of attack.
- **Token type is a claim and is checked.** A refresh token presented as a bearer token is rejected; otherwise stealing one hands the attacker a 14-day access credential.
- **Issuer checked**, so a token from a different service signed with a shared secret does not authenticate here.

## Consequences

### What this makes easy
- Ordinary requests do zero database work for auth.
- A leaked access token dies in ≤15 minutes on its own.
- Refresh-token theft produces a log line (`auth.refresh_reuse_detected`) that can be alerted on.
- "Sign out everywhere" is one UPDATE.

### What this makes hard
- Access tokens remain valid for up to 15 minutes after a ban. Accepted: the alternative is a database lookup per request. A blocklist could close this if it mattered.
- A client that loses the response to a refresh call is holding a revoked token and gets logged out. Mitigated by retrying on the client before rotating.
- Race between two tabs refreshing simultaneously can trip reuse detection. Mitigated by refreshing through a shared worker or a mutex on the client.

## In short

> Fifteen-minute access tokens that are never checked against the database, and
> a fourteen-day refresh token that rotates on every use. The rotation is the
> interesting part: because a refresh token is single-use, replaying one is a
> signal. When I see a revoked token presented, I can't tell whether the
> attacker or the real user is replaying, so I revoke the whole token family
> and force a re-login. That turns a stolen refresh token from fourteen days of
> access into one use plus an alert. The trade-off I accept is that an access
> token stays valid up to fifteen minutes after a ban; closing that would mean
> a lookup per request, which defeats the point of stateless auth.
