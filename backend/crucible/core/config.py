"""Application configuration.

All settings are environment-driven (12-factor). Values are validated once at
import time by pydantic-settings, so a misconfigured deployment fails fast at
boot rather than at first request.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]
SandboxBackend = Literal["docker", "subprocess"]
AIProvider = Literal["gemini", "ollama", "fake"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # ---------------------------------------------------------------- app ---
    environment: Environment = "local"
    debug: bool = False
    app_name: str = "Crucible"
    api_v1_prefix: str = "/api/v1"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # ----------------------------------------------------------- database ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "crucible"
    postgres_password: SecretStr = SecretStr("crucible")
    postgres_db: str = "crucible"
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_echo: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def async_database_url(self) -> str:
        """DSN for the FastAPI process (asyncpg driver)."""
        pw = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{pw}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_database_url(self) -> str:
        """DSN for the queue worker and Alembic (psycopg driver).

        The worker is a synchronous loop; running an asyncio event loop per job
        just to talk to the DB buys nothing and complicates shutdown. We
        deliberately keep two engines -- see docs/adr/0003.
        """
        pw = self.postgres_password.get_secret_value()
        return (
            f"postgresql+psycopg://{self.postgres_user}:{pw}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # ----------------------------------------------------------- security ---
    jwt_secret: SecretStr = SecretStr("dev-only-insecure-secret-do-not-ship-0123456789")
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 14
    password_min_length: int = 10

    @field_validator("jwt_secret")
    @classmethod
    def _validate_jwt_secret(cls, v: SecretStr, info) -> SecretStr:
        """Refuse to boot with a weak signing key outside local development.

        RFC 7518 s3.2 requires an HMAC key at least as long as the hash output
        (32 bytes for SHA-256). A shorter key reduces the effective security of
        every token we issue, so this is a hard failure, not a warning.
        """
        env = (info.data or {}).get("environment")
        secret = v.get_secret_value()

        if env in ("local", "test"):
            return v

        if secret.startswith("dev-only-"):
            raise ValueError("JWT_SECRET is still the development default")
        if len(secret.encode()) < 32:
            raise ValueError(
                "JWT_SECRET must be >= 32 bytes (RFC 7518 s3.2). "
                'Generate one with: python -c "import secrets; '
                'print(secrets.token_urlsafe(48))"'
            )
        return v

    # ------------------------------------------------------------ sandbox ---
    sandbox_backend: SandboxBackend = "docker"
    sandbox_timeout_seconds: int = 10
    sandbox_compile_timeout_seconds: int = 20
    sandbox_memory_mb: int = 256
    sandbox_cpu_quota: float = 1.0
    sandbox_pids_limit: int = 64
    sandbox_tmpfs_mb: int = 32
    sandbox_max_output_bytes: int = 64 * 1024
    sandbox_pool_enabled: bool = True
    sandbox_pool_size_per_language: int = 2
    sandbox_pool_max_reuses: int = 50
    # Only these images are pre-pulled and pooled. Every profile in
    # languages.py still works on demand; this just avoids pulling ~4 GB of
    # toolchains on a laptop to warm languages nobody selected.
    sandbox_enabled_languages: list[str] = Field(
        default_factory=lambda: ["python", "javascript", "cpp"]
    )

    # ------------------------------------------------------------- worker ---
    submission_max_attempts: int = 3

    # ----------------------------------------------------------------- ai ---
    ai_provider: AIProvider = "gemini"
    ai_enabled: bool = True
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-2.0-flash"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    ai_request_timeout_seconds: int = 45
    ai_max_output_tokens: int = 2048
    ai_daily_budget_per_user: int = 50

    # ----------------------------------------------------------- realtime ---
    room_max_participants: int = 4
    room_idle_timeout_seconds: int = 1800

    # ------------------------------------------------------ observability ---
    log_level: str = "INFO"
    log_json: bool = True


@lru_cache
def get_settings() -> Settings:
    """Cached accessor -- import this, never instantiate Settings directly."""
    return Settings()


settings = get_settings()
