"""Authentication: sign-up, sign-in, and refresh-token rotation.

The interesting part is rotation with reuse detection -- see refresh().
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from crucible.core import security
from crucible.core.config import settings
from crucible.core.logging import get_logger
from crucible.db.models import RefreshToken, User
from crucible.schemas.auth import AuthResponse, TokenPair, UserOut

log = get_logger(__name__)


def _invalid_credentials() -> HTTPException:
    # One message for both "no such user" and "wrong password". Distinguishing
    # them turns the login form into an account-enumeration oracle.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "invalid_credentials", "message": "Incorrect email or password"},
    )


async def _issue_tokens(
    db: AsyncSession,
    user: User,
    *,
    family_id: uuid.UUID | None = None,
    user_agent: str | None = None,
    ip: str | None = None,
) -> TokenPair:
    access, _ = security.create_access_token(user_id=user.id, role=user.role.value)
    refresh, expires_at, family = security.create_refresh_token(
        user_id=user.id, role=user.role.value, family_id=family_id
    )
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=security.hash_token(refresh),
            family_id=family,
            expires_at=expires_at,
            user_agent=(user_agent or "")[:255] or None,
            ip_address=(ip or "")[:45] or None,
        )
    )
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_ttl_minutes * 60,
    )


async def sign_up(
    db: AsyncSession,
    *,
    email: str,
    display_name: str,
    password: str,
    user_agent: str | None = None,
    ip: str | None = None,
) -> AuthResponse:
    user = User(
        email=email.lower().strip(),
        display_name=display_name.strip(),
        password_hash=security.hash_password(password),
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        # Race with a concurrent signup, or a duplicate. Same generic message:
        # "email already registered" is itself an enumeration oracle.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "registration_failed",
                "message": "Unable to register with those details",
            },
        ) from exc

    tokens = await _issue_tokens(db, user, user_agent=user_agent, ip=ip)
    log.info("auth.signed_up", user_id=str(user.id))
    return AuthResponse(user=UserOut.model_validate(user), tokens=tokens)


async def sign_in(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    user_agent: str | None = None,
    ip: str | None = None,
) -> AuthResponse:
    result = await db.execute(select(User).where(User.email == email.lower().strip()))
    user = result.scalar_one_or_none()

    if user is None:
        # Burn equivalent CPU so a missing account and a wrong password take
        # the same time. Otherwise response latency enumerates valid emails.
        security.dummy_verify()
        raise _invalid_credentials()

    ok, upgraded_hash = security.verify_password(password, user.password_hash)
    if not ok:
        raise _invalid_credentials()
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "account_disabled", "message": "This account has been disabled"},
        )

    if upgraded_hash:
        # Transparent rehash when cost parameters were raised since signup.
        user.password_hash = upgraded_hash
    user.last_login_at = datetime.now(UTC)

    tokens = await _issue_tokens(db, user, user_agent=user_agent, ip=ip)
    log.info("auth.signed_in", user_id=str(user.id))
    return AuthResponse(user=UserOut.model_validate(user), tokens=tokens)


async def refresh(
    db: AsyncSession,
    *,
    refresh_token: str,
    user_agent: str | None = None,
    ip: str | None = None,
) -> TokenPair:
    """Rotate a refresh token, detecting replay.

    Every refresh mints a new token and revokes the one presented. A token is
    therefore single-use, which gives us a detector: if a *already-revoked*
    token is presented, either an attacker stole it and the legitimate user
    has since rotated, or the reverse. We cannot tell which, so we revoke the
    entire family and force a fresh login. That bounds a stolen token's value
    to a single use rather than its full 14-day lifetime.
    """
    try:
        # Validate signature/expiry/type before touching the database, so a
        # garbage token costs us a HMAC verify rather than an index lookup.
        security.decode_token(refresh_token, expect="refresh")
    except security.AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_refresh_token", "message": str(exc)},
        ) from exc

    token_hash = security.hash_token(refresh_token)
    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    stored = result.scalar_one_or_none()

    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_refresh_token", "message": "Unrecognised refresh token"},
        )

    now = datetime.now(UTC)

    if stored.revoked_at is not None:
        # Replay of a rotated token. Burn the family.
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == stored.family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        # Commit BEFORE raising. The request-scoped session rolls back on any
        # exception, so without this explicit commit the 401 below undoes the
        # revocation we just performed -- the detection would log a warning,
        # return an error, and leave every stolen token in the family still
        # valid. The security action has to outlive the error path.
        await db.commit()

        log.warning(
            "auth.refresh_reuse_detected",
            user_id=str(stored.user_id),
            family_id=str(stored.family_id),
            action="revoked entire token family",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "refresh_token_reused",
                "message": "Session revoked for security. Please sign in again.",
            },
        )

    if stored.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "refresh_token_expired", "message": "Session expired"},
        )

    user = await db.get(User, stored.user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_refresh_token", "message": "Account unavailable"},
        )

    stored.revoked_at = now
    tokens = await _issue_tokens(db, user, family_id=stored.family_id, user_agent=user_agent, ip=ip)
    await db.flush()

    new_hash = security.hash_token(tokens.refresh_token)
    new_row = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == new_hash))
    replacement = new_row.scalar_one_or_none()
    if replacement is not None:
        stored.replaced_by = replacement.id

    return tokens


async def sign_out(db: AsyncSession, *, refresh_token: str) -> None:
    """Revoke one session.

    Only the presented token's family is revoked, so signing out on a phone
    does not sign the user out on their laptop.
    """
    token_hash = security.hash_token(refresh_token)
    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    stored = result.scalar_one_or_none()
    if stored is None:
        return  # already gone; nothing to report
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == stored.family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def sign_out_everywhere(db: AsyncSession, *, user_id: uuid.UUID) -> int:
    """Revoke every session for a user (password change, or 'log out all')."""
    result = await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
        .returning(RefreshToken.id)
    )
    return len(result.all())
