"""Request and response contracts for slot search and booking."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

from app.models.enums import AppointmentStatus

SYMPTOMS_MIN_LENGTH = 10
SYMPTOMS_MAX_LENGTH = 4000


class SlotResponse(BaseModel):
    starts_at: datetime
    ends_at: datetime


class AvailabilityResponse(BaseModel):
    doctor_id: uuid.UUID
    date: date
    slot_duration_minutes: int
    timezone: str = Field(description="The clinic timezone the working day is defined in.")
    slots: list[SlotResponse]


class HoldRequest(BaseModel):
    doctor_id: uuid.UUID
    starts_at: datetime = Field(
        description="Slot start as returned by the availability endpoint, with a UTC offset."
    )

    @field_validator("starts_at")
    @classmethod
    def _require_offset(cls, value: datetime) -> datetime:
        # A naive datetime would be ambiguous: the same wall-clock string means different
        # instants depending on who sent it.
        if value.tzinfo is None:
            raise ValueError("starts_at must include a timezone offset, e.g. 2026-09-01T09:00:00Z")
        return value


class SymptomFormRequest(BaseModel):
    """What the patient reports before the appointment is confirmed.

    Treated as untrusted input throughout: it is stored verbatim, and it becomes part of an
    LLM prompt in Phase 6.
    """

    symptoms: str = Field(min_length=SYMPTOMS_MIN_LENGTH, max_length=SYMPTOMS_MAX_LENGTH)
    duration_days: int | None = Field(default=None, ge=0, le=3650)
    additional_notes: str | None = Field(default=None, max_length=SYMPTOMS_MAX_LENGTH)

    @field_validator("symptoms")
    @classmethod
    def _strip_symptoms(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < SYMPTOMS_MIN_LENGTH:
            raise ValueError(
                f"Please describe the symptoms in at least {SYMPTOMS_MIN_LENGTH} characters."
            )
        return stripped


class CancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=255)


class RescheduleRequest(BaseModel):
    starts_at: datetime

    @field_validator("starts_at")
    @classmethod
    def _require_offset(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("starts_at must include a timezone offset, e.g. 2026-09-01T09:00:00Z")
        return value


class DoctorSummary(BaseModel):
    id: uuid.UUID
    full_name: str
    specialisation: str
    slot_duration_minutes: int


class PatientSummary(BaseModel):
    id: uuid.UUID
    full_name: str


class SymptomReportResponse(BaseModel):
    symptoms: str
    duration_days: int | None
    additional_notes: str | None


class AppointmentResponse(BaseModel):
    id: uuid.UUID
    status: AppointmentStatus
    starts_at: datetime
    ends_at: datetime
    doctor: DoctorSummary
    patient: PatientSummary
    hold_expires_at: datetime | None = Field(
        default=None, description="Present only while the slot is held pending confirmation."
    )
    cancellation_reason: str | None = None
    symptom_report: SymptomReportResponse | None = None
