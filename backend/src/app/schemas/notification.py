"""Response contracts for the notification outbox admin view."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import NotificationStatus, NotificationType


class NotificationJobResponse(BaseModel):
    id: uuid.UUID
    notification_type: NotificationType
    status: NotificationStatus
    recipient_email: str
    appointment_id: uuid.UUID | None
    scheduled_for: datetime
    attempts: int
    next_attempt_at: datetime | None
    sent_at: datetime | None
    last_error: str | None = Field(
        default=None, description="Why the last attempt failed. Kept so a failure is diagnosable."
    )


class NotificationSummaryResponse(BaseModel):
    pending: int
    sent: int
    failed: int = Field(description="Exhausted their retries and need a human.")
