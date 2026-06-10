from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from crucible.api.deps import CurrentUser, DbSession
from crucible.schemas.auth import (
    AuthResponse,
    RefreshRequest,
    SignInRequest,
    SignUpRequest,
    TokenPair,
    UserOut,
)
from crucible.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _client(request: Request) -> tuple[str | None, str | None]:
    """Best-effort client fingerprint for the session record.

    X-Forwarded-For is attacker-controlled unless a trusted proxy sets it, so
    this is recorded for the user's own "active sessions" view -- never used
    for an authorisation decision.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = (
        forwarded.split(",")[0].strip()
        if forwarded
        else (request.client.host if request.client else None)
    )
    return request.headers.get("user-agent"), ip


@router.post(
    "/signup",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
)
async def sign_up(payload: SignUpRequest, request: Request, db: DbSession) -> AuthResponse:
    ua, ip = _client(request)
    return await auth_service.sign_up(
        db,
        email=payload.email,
        display_name=payload.display_name,
        password=payload.password,
        user_agent=ua,
        ip=ip,
    )


@router.post("/signin", response_model=AuthResponse, summary="Sign in")
async def sign_in(payload: SignInRequest, request: Request, db: DbSession) -> AuthResponse:
    ua, ip = _client(request)
    return await auth_service.sign_in(
        db, email=payload.email, password=payload.password, user_agent=ua, ip=ip
    )


@router.post(
    "/refresh",
    response_model=TokenPair,
    summary="Rotate an access token",
    description=(
        "Exchanges a refresh token for a new pair. The presented token is "
        "revoked. Replaying a revoked token revokes the entire family and "
        "forces a fresh sign-in."
    ),
)
async def refresh(payload: RefreshRequest, request: Request, db: DbSession) -> TokenPair:
    ua, ip = _client(request)
    return await auth_service.refresh(db, refresh_token=payload.refresh_token, user_agent=ua, ip=ip)


@router.post("/signout", status_code=status.HTTP_204_NO_CONTENT, summary="End this session")
async def sign_out(payload: RefreshRequest, db: DbSession) -> Response:
    await auth_service.sign_out(db, refresh_token=payload.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/signout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="End every session for this account",
)
async def sign_out_all(user: CurrentUser, db: DbSession) -> Response:
    await auth_service.sign_out_everywhere(db, user_id=user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserOut, summary="Current user")
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
