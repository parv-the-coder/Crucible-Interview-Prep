from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from crucible.core.config import settings
from crucible.db.enums import UserRole
from crucible.schemas.common import ORMModel


class SignUpRequest(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=10, max_length=256)

    @field_validator("password")
    @classmethod
    def _strength(cls, v: str) -> str:
        """Length first, composition second.

        NIST SP 800-63B advises against forced character-class rules -- they
        push users toward "Password1!" and nothing else. We require real length
        and screen the handful of passwords everyone actually tries.
        """
        if len(v) < settings.password_min_length:
            raise ValueError(f"Password must be at least {settings.password_min_length} characters")
        lowered = v.lower()
        banned = {"password", "12345678", "qwertyuiop", "letmein", "iloveyou", "admin123"}
        if lowered in banned or any(b in lowered for b in ("password1", "123456789")):
            raise ValueError("That password appears in common breach lists")
        if len(set(v)) < 5:
            raise ValueError("Password is too repetitive")
        return v


class SignInRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 scheme name, not a secret
    expires_in: int = Field(description="Access token lifetime in seconds.")


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: UserRole
    rating: float
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None


class AuthResponse(BaseModel):
    user: UserOut
    tokens: TokenPair
