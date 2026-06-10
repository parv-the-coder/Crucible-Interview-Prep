"""Auth primitives.

Each test pins a property an attacker would otherwise exploit.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from crucible.core import security
from crucible.core.config import Settings, settings

# ------------------------------------------------------------- passwords ---


def test_hash_is_argon2id_and_salted() -> None:
    h1 = security.hash_password("hunter2hunter2")
    h2 = security.hash_password("hunter2hunter2")
    assert h1.startswith("$argon2id$")
    # Same password, different hash: the salt is per-hash, so a stolen table
    # cannot be attacked with one precomputed rainbow table.
    assert h1 != h2


def test_verify_accepts_the_right_password_and_rejects_others() -> None:
    h = security.hash_password("correct horse battery staple")
    assert security.verify_password("correct horse battery staple", h)[0] is True
    assert security.verify_password("Correct horse battery staple", h)[0] is False
    assert security.verify_password("", h)[0] is False


def test_verify_does_not_raise_on_a_corrupt_stored_hash() -> None:
    """A malformed row must fail the login, not 500 the endpoint."""
    ok, _ = security.verify_password("anything", "not-a-hash")
    assert ok is False


def test_long_passwords_are_not_truncated() -> None:
    """bcrypt silently ignores everything past 72 bytes; argon2 must not.

    Under bcrypt these two 100-char passwords sharing a 72-char prefix would
    verify against each other's hash.
    """
    base = "x" * 72
    h = security.hash_password(base + "AAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    assert security.verify_password(base + "BBBBBBBBBBBBBBBBBBBBBBBBBBBB", h)[0] is False


def test_dummy_verify_costs_roughly_a_real_verify() -> None:
    """Equalises timing so a wrong email and a wrong password look the same."""
    h = security.hash_password("some password here")

    start = time.perf_counter()
    security.verify_password("wrong password here", h)
    real = time.perf_counter() - start

    start = time.perf_counter()
    security.dummy_verify()
    dummy = time.perf_counter() - start

    # Same order of magnitude is what defeats timing-based user enumeration.
    assert 0.2 < (dummy / real) < 5.0, f"real={real:.4f}s dummy={dummy:.4f}s"


# ---------------------------------------------------------------- tokens ---


def test_access_token_round_trips() -> None:
    uid = uuid.uuid4()
    token, expires = security.create_access_token(user_id=uid, role="admin")
    claims = security.decode_token(token, expect="access")
    assert claims.subject == uid
    assert claims.role == "admin"
    assert claims.token_type == "access"
    assert expires > datetime.now(UTC)


def test_refresh_token_carries_a_family_id() -> None:
    uid = uuid.uuid4()
    token, _, family = security.create_refresh_token(user_id=uid, role="student")
    claims = security.decode_token(token, expect="refresh")
    assert claims.family_id == family


def test_refresh_token_is_rejected_where_an_access_token_is_required() -> None:
    """Otherwise a stolen refresh token is a 14-day bearer credential."""
    token, _, _ = security.create_refresh_token(user_id=uuid.uuid4(), role="student")
    with pytest.raises(security.AuthError, match="expected access"):
        security.decode_token(token, expect="access")


def test_expired_token_is_rejected() -> None:
    payload = {
        "sub": str(uuid.uuid4()),
        "role": "student",
        "type": "access",
        "jti": str(uuid.uuid4()),
        "iat": int((datetime.now(UTC) - timedelta(hours=2)).timestamp()),
        "exp": int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
        "iss": settings.app_name.lower(),
    }
    token = jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm="HS256")
    with pytest.raises(security.AuthError, match="expired"):
        security.decode_token(token)


def test_token_signed_with_another_key_is_rejected() -> None:
    payload = {
        "sub": str(uuid.uuid4()),
        "role": "admin",
        "type": "access",
        "jti": str(uuid.uuid4()),
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        "iss": settings.app_name.lower(),
    }
    forged = jwt.encode(payload, "attacker-key-attacker-key-attacker", algorithm="HS256")
    with pytest.raises(security.AuthError):
        security.decode_token(forged)


def test_alg_none_token_is_rejected() -> None:
    """The classic JWT bypass: strip the signature and claim alg=none."""
    payload = {
        "sub": str(uuid.uuid4()),
        "role": "admin",
        "type": "access",
        "jti": str(uuid.uuid4()),
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        "iss": settings.app_name.lower(),
    }
    unsigned = jwt.encode(payload, key="", algorithm="none")
    with pytest.raises(security.AuthError):
        security.decode_token(unsigned)


def test_token_from_another_issuer_is_rejected() -> None:
    payload = {
        "sub": str(uuid.uuid4()),
        "role": "admin",
        "type": "access",
        "jti": str(uuid.uuid4()),
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        "iss": "some-other-service",
    }
    token = jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm="HS256")
    with pytest.raises(security.AuthError):
        security.decode_token(token)


def test_token_missing_required_claims_is_rejected() -> None:
    token = jwt.encode(
        {"sub": str(uuid.uuid4())}, settings.jwt_secret.get_secret_value(), algorithm="HS256"
    )
    with pytest.raises(security.AuthError):
        security.decode_token(token)


def test_refresh_tokens_are_stored_only_as_hashes() -> None:
    token, _, _ = security.create_refresh_token(user_id=uuid.uuid4(), role="student")
    digest = security.hash_token(token)
    assert len(digest) == 64
    assert token not in digest
    assert security.hash_token(token) == digest  # deterministic lookup key


# ---------------------------------------------------------------- config ---


@pytest.mark.parametrize(
    "secret",
    ["dev-only-insecure-secret-do-not-ship-0123456789", "short", ""],
)
def test_production_refuses_a_weak_signing_key(secret: str) -> None:
    with pytest.raises(ValueError):
        Settings(environment="production", jwt_secret=secret)


def test_production_accepts_a_strong_signing_key() -> None:
    strong = "k" * 48
    assert Settings(environment="production", jwt_secret=strong).environment == "production"


# ------------------------------------------------------------- join codes ---


def test_join_codes_avoid_ambiguous_characters() -> None:
    """Codes get read aloud on a call; 0/O and 1/I/L are how that goes wrong."""
    for _ in range(200):
        code = security.generate_join_code()
        assert not set(code) & set("01ILO")


def test_join_codes_do_not_repeat() -> None:
    codes = {security.generate_join_code() for _ in range(2000)}
    assert len(codes) == 2000
