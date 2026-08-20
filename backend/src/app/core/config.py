"""Application settings, loaded from the environment with fail-fast validation.

Settings are read once at startup and injected via `get_settings()`. Anything missing or
malformed raises immediately rather than surfacing as a confusing error deep in a request.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "test", "prod"]

_ASYNC_DRIVER = "postgresql+psycopg"


def _normalise_pg_url(value: str) -> str:
    """Force the async psycopg driver onto a Postgres URL.

    Managed providers hand out `postgres://` or `postgresql://` URLs. SQLAlchemy needs an
    explicit async driver, and forgetting to rewrite the prefix is a classic deploy-day
    failure, so every URL is normalised on the way in.
    """
    for prefix in ("postgres://", "postgresql://"):
        if value.startswith(prefix):
            return f"{_ASYNC_DRIVER}://{value[len(prefix) :]}"
    return value


def mask_url_password(url: str) -> str:
    """Return `url` with any password replaced, so it is safe to log."""
    parts = urlsplit(url)
    if parts.password is None:
        return url
    userinfo = f"{parts.username}:***" if parts.username else "***"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"{userinfo}@{host}", parts.path, parts.query, parts.fragment))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Environment = "dev"
    log_level: str = "INFO"

    database_url: str
    test_database_url: str | None = None
    # Without an explicit timeout a TCP connect to an unreachable database can hang
    # indefinitely instead of being refused, wedging the request that triggered it.
    db_connect_timeout_seconds: int = Field(default=10, gt=0)

    jwt_secret: SecretStr
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = Field(default=60, gt=0)

    # Comma-separated rather than a JSON list: pydantic-settings parses list-typed fields as
    # JSON, which makes a plain `A,B` value in a hosting dashboard fail in a confusing way.
    cors_origins: str = "http://localhost:5173"

    @field_validator("database_url", "test_database_url", mode="before")
    @classmethod
    def _force_async_driver(cls, value: str | None) -> str | None:
        return _normalise_pg_url(value) if isinstance(value, str) else value

    @field_validator("jwt_secret")
    @classmethod
    def _reject_placeholder_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError(
                "JWT_SECRET must be at least 32 characters. "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "prod"

    @property
    def safe_database_url(self) -> str:
        """Password-masked database URL for logging."""
        return mask_url_password(self.database_url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, parsed once. Values come from the environment/.env."""
    return Settings()
