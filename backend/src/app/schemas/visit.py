"""Contracts for recording a visit and reading its summaries."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.models.enums import SummaryStatus, SummaryType, UrgencyLevel

NOTES_MIN_LENGTH = 20
NOTES_MAX_LENGTH = 8000


class MedicationRequest(BaseModel):
    """One prescribed medicine.

    Structured rather than free text because these fields drive the reminder schedule. The
    bounds match the database check constraints.
    """

    drug_name: str = Field(min_length=1, max_length=200)
    dosage: str = Field(min_length=1, max_length=100)
    times_per_day: int = Field(ge=1, le=12)
    duration_days: int = Field(ge=1, le=365)
    instructions: str | None = Field(default=None, max_length=255)


class RecordVisitRequest(BaseModel):
    clinical_notes: str = Field(min_length=NOTES_MIN_LENGTH, max_length=NOTES_MAX_LENGTH)
    medications: list[MedicationRequest] = Field(default_factory=list, max_length=20)
    follow_up_date: date | None = None

    @field_validator("clinical_notes")
    @classmethod
    def _strip_notes(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < NOTES_MIN_LENGTH:
            raise ValueError(f"clinical notes must be at least {NOTES_MIN_LENGTH} characters")
        return stripped


class SummaryResponse(BaseModel):
    """A generated summary, or an honest statement that it is not ready.

    `status` is part of the contract rather than an implementation detail: a doctor seeing
    "being prepared" understands something different from an empty brief.
    """

    summary_type: SummaryType
    status: SummaryStatus
    urgency: UrgencyLevel | None = None
    content: dict[str, Any] | None = None
    prompt_version: str
    model: str | None = None
    attempts: int
    unavailable_reason: str | None = Field(
        default=None, description="Set only when the summary could not be generated."
    )


class RecordVisitResponse(BaseModel):
    appointment_id: uuid.UUID
    status: str
    completed_at: datetime
    reminders_scheduled: int = Field(
        description="Medication reminders queued from the prescription's structured fields."
    )
