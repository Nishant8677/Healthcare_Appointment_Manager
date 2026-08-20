"""Request and response bodies for calendar connection and sync."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import CalendarSyncAction, CalendarSyncStatus


class CalendarAuthorizationResponse(BaseModel):
    """Where to send the user to grant access."""

    authorization_url: str = Field(
        description="Open this in a browser. Google returns the user to the callback endpoint."
    )
    expires_in_minutes: int = Field(
        description="How long the signed state in that URL stays valid."
    )


class CalendarConnectionResponse(BaseModel):
    """The state of one user's calendar link.

    Never carries a token — not the refresh token, not an access token. The only thing a
    client needs is whether it works and which account it points at.
    """

    model_config = ConfigDict(from_attributes=True)

    connected: bool = Field(description="True when events are currently being written.")
    google_account_email: str | None = None
    calendar_id: str | None = None
    connected_at: datetime | None = None
    revoked_at: datetime | None = Field(
        default=None,
        description="Set when the grant was withdrawn; the user needs to reconnect.",
    )
    last_error: str | None = None


class CalendarCallbackResponse(BaseModel):
    """Returned by the OAuth callback when no browser redirect is configured."""

    connected: bool
    google_account_email: str
    appointments_queued: int = Field(
        description="Existing upcoming appointments queued for this calendar on connect."
    )


class CalendarSyncJobResponse(BaseModel):
    """One row of the reconciler's desired state, for admin visibility."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    appointment_id: uuid.UUID
    user_id: uuid.UUID
    action: CalendarSyncAction
    status: CalendarSyncStatus
    google_event_id: str
    calendar_id: str
    attempts: int
    next_attempt_at: datetime | None
    synced_at: datetime | None
    last_error: str | None


class CalendarSyncSummaryResponse(BaseModel):
    """Counts by state. `skipped` is expected and healthy; `failed` is the one to watch."""

    pending: int
    synced: int
    skipped: int
    failed: int
