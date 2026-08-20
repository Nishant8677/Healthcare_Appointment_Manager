"""Request and response contracts for doctor management."""

from __future__ import annotations

import uuid
from datetime import date, time

from pydantic import BaseModel, EmailStr, Field, SecretStr, field_validator, model_validator

from app.schemas.auth import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH

# A consultation longer than four hours is far more likely to be a typo than a real clinic
# policy; the same bound is enforced by a database check constraint.
SLOT_DURATION_MIN = 5
SLOT_DURATION_MAX = 240


class WorkingHoursItem(BaseModel):
    """One window of availability on one weekday."""

    weekday: int = Field(ge=0, le=6, description="0 = Monday … 6 = Sunday.")
    start_time: time
    end_time: time

    @model_validator(mode="after")
    def _end_after_start(self) -> WorkingHoursItem:
        # A fast, obvious rejection here; the full schedule (overlaps, slot divisibility) is
        # checked in the domain layer where the doctor's slot duration is known.
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be later than start_time")
        return self


class DoctorCreateRequest(BaseModel):
    """Create a doctor's login and clinic profile together."""

    email: EmailStr
    password: SecretStr = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)
    full_name: str = Field(min_length=1, max_length=200)
    specialisation: str = Field(min_length=1, max_length=120)
    slot_duration_minutes: int = Field(ge=SLOT_DURATION_MIN, le=SLOT_DURATION_MAX)
    working_hours: list[WorkingHoursItem] = Field(
        default_factory=list,
        description="Optional at creation; set later with PUT /admin/doctors/{id}/working-hours.",
    )

    @field_validator("full_name", "specialisation")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class DoctorUpdateRequest(BaseModel):
    """Partial update. Omitted fields are left unchanged."""

    specialisation: str | None = Field(default=None, min_length=1, max_length=120)
    slot_duration_minutes: int | None = Field(
        default=None, ge=SLOT_DURATION_MIN, le=SLOT_DURATION_MAX
    )
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    is_active: bool | None = None

    @field_validator("full_name", "specialisation")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> DoctorUpdateRequest:
        if not self.model_dump(exclude_none=True):
            raise ValueError("provide at least one field to update")
        return self


class WorkingHoursReplaceRequest(BaseModel):
    """The doctor's complete weekly availability.

    Replaces the schedule wholesale rather than editing rows individually: overlap is a
    property of the whole set, so validating a full replacement is both simpler and safer
    than reasoning about the intermediate states of a partial edit.
    """

    working_hours: list[WorkingHoursItem]


class LeaveDayCreateRequest(BaseModel):
    leave_date: date
    reason: str | None = Field(default=None, max_length=255)


class WorkingHoursResponse(BaseModel):
    id: uuid.UUID
    weekday: int
    start_time: time
    end_time: time


class LeaveDayResponse(BaseModel):
    id: uuid.UUID
    leave_date: date
    reason: str | None


class DoctorResponse(BaseModel):
    """Summary view, flattened across the user account and the clinic profile."""

    id: uuid.UUID
    user_id: uuid.UUID
    full_name: str
    email: EmailStr
    specialisation: str
    slot_duration_minutes: int
    is_active: bool


class DoctorDetailResponse(DoctorResponse):
    working_hours: list[WorkingHoursResponse]
    leave_days: list[LeaveDayResponse]
