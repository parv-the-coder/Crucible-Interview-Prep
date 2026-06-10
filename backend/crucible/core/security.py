"""Password hashing and JWT issuance/verification.

Threat model notes that drove these choices are in docs/05-security.md.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type

from crucible.core.config import settings

TokenType = Literal["access", "refresh"]

# Argon2id, not bcrypt. bcrypt silently truncates at 72 bytes and is only
# memory-light, which is exactly what GPU cracking rigs want. These parameters
# target ~50-100ms per hash on commodity hardware -- painful to brute force,
# invisible to a user logging in.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,  # 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


class AuthError(Exception):
    """Raised for any credential or token failure."""


# --------------------------------------------------------------- passwords ---


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> tuple[bool, str | None]:
    """Verify a password.

    Returns ``(ok, new_hash)``. ``new_hash`` is non-None when the stored hash
    used weaker parameters than we now require, so the caller can transparently
    upgrade it on a successful login.
    """
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False, None
    except Exception:
        return False, None

    if _hasher.check_needs_rehash(password_hash):
        return True, _hasher.hash(password)
    return True, None


def dummy_verify() -> None:
    """Burn the same CPU as a real verify for a non-existent account.

    Without this, "no such user" returns in microseconds while a wrong password
    takes ~80ms, and that timing difference enumerates valid accounts.
    """
    _hasher.verify(
        _hasher.hash("timing-equalisation-placeholder"),
        "timing-equalisation-placeholder",
    )


# ------------------------------------------------------------------ tokens ---


@dataclass(frozen=True, slots=True)
class TokenClaims:
    subject: uuid.UUID
    role: str
    token_type: TokenType
    jti: uuid.UUID
    family_id: uuid.UUID | None
    expires_at: datetime
    issued_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


def create_access_token(
    *, user_id: uuid.UUID, role: str, extra: dict[str, Any] | None = None
) -> tuple[str, datetime]:
    """Short-lived bearer token. Deliberately 15 minutes.

    Access tokens are not revocable without a blocklist lookup on every
    request, which defeats the point of stateless auth. We keep the window
    small instead and put revocation on the refresh token, which *is* checked
    against the database.
    """
    issued = _now()
    expires = issued + timedelta(minutes=settings.access_token_ttl_minutes)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "jti": str(uuid.uuid4()),
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "iss": settings.app_name.lower(),
    }
    if extra:
        payload.update(extra)
    token = jwt.encode(
        payload, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    return token, expires


def create_refresh_token(
    *, user_id: uuid.UUID, role: str, family_id: uuid.UUID | None = None
) -> tuple[str, datetime, uuid.UUID]:
    """Long-lived rotating token. Returns ``(token, expires_at, family_id)``."""
    issued = _now()
    expires = issued + timedelta(days=settings.refresh_token_ttl_days)
    family = family_id or uuid.uuid4()
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": "refresh",
        "jti": str(uuid.uuid4()),
        "fam": str(family),
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "iss": settings.app_name.lower(),
    }
    token = jwt.encode(
        payload, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    return token, expires, family


def decode_token(token: str, *, expect: TokenType | None = None) -> TokenClaims:
    """Decode and validate a JWT, raising AuthError on any problem."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],  # never trust the header's alg
            issuer=settings.app_name.lower(),
            options={"require": ["exp", "iat", "sub", "type", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("invalid token") from exc

    token_type = payload.get("type")
    if expect is not None and token_type != expect:
        # Stops a refresh token being replayed as a bearer token, which would
        # hand an attacker a 14-day access credential.
        raise AuthError(f"expected {expect} token, got {token_type}")

    try:
        subject = uuid.UUID(payload["sub"])
        jti = uuid.UUID(payload["jti"])
    except (KeyError, ValueError) as exc:
        raise AuthError("malformed token claims") from exc

    family_raw = payload.get("fam")
    return TokenClaims(
        subject=subject,
        role=str(payload.get("role", "student")),
        token_type=token_type,  # type: ignore[arg-type]
        jti=jti,
        family_id=uuid.UUID(family_raw) if family_raw else None,
        expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        issued_at=datetime.fromtimestamp(payload["iat"], tz=UTC),
    )


def hash_token(token: str) -> str:
    """SHA-256 of a refresh token.

    The database stores only this. A dump of refresh_tokens is then useless to
    an attacker -- they cannot reverse it into a usable credential. SHA-256 is
    correct here (not Argon2): the input is 300+ bits of our own entropy, so
    there is no dictionary to attack and we want the lookup to be fast.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def generate_join_code(length: int = 7) -> str:
    """Room join code from an unambiguous alphabet.

    Crockford-style: no I/L/O/0/1, because these get read aloud over a call.
    32^7 is ~34 billion, and codes are single-use and short-lived, so guessing
    is not a practical attack.
    """
    alphabet = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
    raw = "".join(secrets.choice(alphabet) for _ in range(length))
    return f"{raw[:3]}-{raw[3:]}"
