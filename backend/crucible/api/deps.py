"""FastAPI dependencies: auth, roles and rate limiting."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from crucible.core import security
from crucible.core.logging import user_id_ctx
from crucible.db.enums import UserRole
from crucible.db.models import User
from crucible.db.session import get_db

# auto_error=False so a missing header produces our JSON error envelope
# instead of FastAPI's, keeping every 401 the same shape for clients.
bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_db)]


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "unauthenticated", "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    request: Request,
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    if credentials is None or not credentials.credentials:
        raise _unauthorized("Missing bearer token")

    try:
        claims = security.decode_token(credentials.credentials, expect="access")
    except security.AuthError as exc:
        raise _unauthorized(str(exc)) from exc

    user = await db.get(User, claims.subject)
    if user is None:
        # The token verifies but the account is gone. Treat as unauthenticated,
        # never as an internal error.
        raise _unauthorized("Account no longer exists")
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "account_disabled", "message": "This account has been disabled"},
        )

    # Bind for logging and for the rate limiter's bucket key.
    user_id_ctx.set(str(user.id))
    request.state.user = user
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*roles: UserRole):
    """Dependency factory for role gates.

    Admin passes every gate: encoding that here means no endpoint can forget
    to include it, which is how privilege checks drift apart.
    """

    async def _guard(user: CurrentUser) -> User:
        if user.role is UserRole.ADMIN or user.role in roles:
            return user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "insufficient_role",
                "message": f"Requires one of: {', '.join(r.value for r in roles)}",
            },
        )

    return _guard


RequireAdmin = Annotated[User, Depends(require_role(UserRole.ADMIN))]
RequireInterviewer = Annotated[User, Depends(require_role(UserRole.INTERVIEWER))]


async def get_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str | None:
    """Read and validate the Idempotency-Key header.

    Bounded length because it becomes part of a unique index; an unbounded
    header would let a client blow the btree row limit.
    """
    if idempotency_key is None:
        return None
    key = idempotency_key.strip()
    if not key:
        return None
    if len(key) > 64:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_idempotency_key", "message": "Key must be <= 64 characters"},
        )
    return key
