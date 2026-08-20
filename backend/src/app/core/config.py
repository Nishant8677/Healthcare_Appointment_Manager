"""Application settings, loaded from the environment with fail-fast validation.

Settings are read once at startup and injected via `get_settings()`. Anything missing or
malformed raises immediately rather than surfacing as a confusing error deep in a request.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
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

    # Doctors' working hours are wall-clock times with no zone of their own; this is the zone
    # they are read in. Appointments themselves are always stored as UTC instants.
    clinic_timezone: str = "UTC"
    # How long a slot stays reserved while the patient completes the symptom form.
    slot_hold_minutes: int = Field(default=5, gt=0, le=60)
    # How far ahead patients may book. Bounds slot generation and stops a booking being made
    # years out, before the clinic's hours for that period are known.
    booking_horizon_days: int = Field(default=60, gt=0, le=365)
    # How long before an appointment its reminder is sent.
    reminder_lead_hours: int = Field(default=24, gt=0, le=168)

    # --- Email delivery ---
    # "console" logs the message instead of sending, which is what local development and
    # the test suite use. Nothing is ever sent to a real address by accident.
    email_provider: Literal["console", "sendgrid"] = "console"
    email_api_key: SecretStr | None = None
    email_from: str = "clinic@example.com"
    email_from_name: str = "The Clinic"
    email_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    # --- LLM summaries ---
    # "stub" returns a deterministic canned summary: the default for local development and
    # tests, so no API key is needed and no request is ever billed by accident.
    llm_provider: Literal["stub", "anthropic"] = "stub"
    llm_api_key: SecretStr | None = None
    llm_model: str = "claude-opus-5"
    llm_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    llm_max_output_tokens: int = Field(default=2000, gt=0, le=16000)
    # Attempts before a summary is parked as failed. A summary is never on the critical path,
    # so failing one has no effect on the appointment itself.
    llm_max_attempts: int = Field(default=4, gt=0, le=10)

    # --- Medication reminders ---
    # Reminders are generated from the prescription's structured fields, never from LLM text.
    # Long courses are capped so one prescription cannot queue thousands of messages.
    medication_reminder_max_days: int = Field(default=14, gt=0, le=90)
    # Doses are spread across this waking window in the clinic's timezone.
    medication_first_dose_hour: int = Field(default=8, ge=0, le=23)
    medication_last_dose_hour: int = Field(default=20, ge=0, le=23)

    # --- Notification worker ---
    # --- Google Calendar ---
    # Unset means the feature is inert rather than broken: no user can connect a calendar,
    # so no sync row is ever written and every other part of the system behaves normally.
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    # Must match a redirect URI registered on the OAuth client exactly, character for
    # character, or Google rejects the authorisation request with redirect_uri_mismatch.
    google_redirect_uri: str = "http://localhost:8000/calendar/callback"
    # Fernet key encrypting refresh tokens at rest. A refresh token is a long-lived key to
    # somebody's calendar; a database dump must not hand them over in plaintext.
    calendar_token_key: SecretStr | None = None
    google_api_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    # How long a signed OAuth state token stays valid. Short: it only has to survive the
    # user clicking through Google's consent screen.
    calendar_state_ttl_minutes: int = Field(default=10, gt=0, le=60)
    # Where to send the browser after the callback succeeds. Unset returns JSON instead,
    # which is what makes the flow testable with curl before any frontend exists.
    calendar_return_url: str | None = None
    calendar_poll_seconds: float = Field(default=20.0, gt=0, le=300)
    calendar_batch_size: int = Field(default=20, gt=0, le=200)
    calendar_max_attempts: int = Field(default=5, gt=0, le=10)
    # How far back to backfill when a user connects their calendar. Bounded so connecting
    # cannot queue an unbounded amount of work.
    calendar_backfill_limit: int = Field(default=50, gt=0, le=500)

    # --- Notification worker ---
    # Gates every background worker, not only notifications: the API and the workers run
    # in one process by default, and a deployment that scales the API out can turn them
    # off on the web instances and run one worker instance instead.
    background_workers_enabled: bool = True
    notification_poll_seconds: float = Field(default=15.0, gt=0, le=300)
    notification_batch_size: int = Field(default=20, gt=0, le=200)
    # Attempts before a job is parked as failed for a human to look at.
    notification_max_attempts: int = Field(default=4, gt=0, le=10)
    summary_poll_seconds: float = Field(default=20.0, gt=0, le=300)
    summary_batch_size: int = Field(default=10, gt=0, le=100)

    # Comma-separated rather than a JSON list: pydantic-settings parses list-typed fields as
    # JSON, which makes a plain `A,B` value in a hosting dashboard fail in a confusing way.
    cors_origins: str = "http://localhost:5173"

    @field_validator("database_url", "test_database_url", mode="before")
    @classmethod
    def _force_async_driver(cls, value: str | None) -> str | None:
        return _normalise_pg_url(value) if isinstance(value, str) else value

    @field_validator("clinic_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"{value!r} is not a known IANA timezone (for example 'Asia/Kolkata')."
            ) from exc
        return value

    @field_validator("jwt_secret")
    @classmethod
    def _reject_placeholder_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError(
                "JWT_SECRET must be at least 32 characters. "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return value

    @model_validator(mode="after")
    def _calendar_credentials_are_complete(self) -> Settings:
        """Reject a half-configured Google integration at startup.

        A client id without a secret, or without an encryption key for the refresh tokens it
        will produce, only fails later — at the callback, after the user has already granted
        consent. That is the worst moment to discover a missing environment variable.
        """
        if self.google_client_id is None:
            return self
        missing = [
            name
            for name, value in (
                ("GOOGLE_CLIENT_SECRET", self.google_client_secret),
                ("CALENDAR_TOKEN_KEY", self.calendar_token_key),
            )
            if value is None
        ]
        if missing:
            verb = "is" if len(missing) == 1 else "are"
            raise ValueError(
                f"GOOGLE_CLIENT_ID is set but {' and '.join(missing)} {verb} not. "
                "Generate a token key with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
        return self

    @property
    def calendar_enabled(self) -> bool:
        """True when a user could actually connect a Google Calendar."""
        return self.google_client_id is not None

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def clinic_zone(self) -> ZoneInfo:
        return ZoneInfo(self.clinic_timezone)

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
