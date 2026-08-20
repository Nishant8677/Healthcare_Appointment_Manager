"""Domain enumerations.

Collected in one module because several are referenced across otherwise unrelated tables,
and because a reviewer reading the schema should be able to see every legal state in one
place. All are stored as native PostgreSQL enum types.
"""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    PATIENT = "patient"
    DOCTOR = "doctor"
    ADMIN = "admin"


class AppointmentStatus(StrEnum):
    """Lifecycle of a booking.

    `HELD` is a short-lived reservation taken while the patient completes the symptom form;
    it becomes `CONFIRMED` on submission or lapses once `hold_expires_at` passes. The two
    cancellation states are distinct because a clinic-initiated cancellation (doctor leave)
    triggers a different notification and gives the patient a rebooking link.
    """

    HELD = "held"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED_BY_PATIENT = "cancelled_by_patient"
    CANCELLED_BY_CLINIC = "cancelled_by_clinic"


# Statuses that occupy a slot. Anything outside this set frees the time for rebooking, and
# the partial unique index on `appointments` is defined over exactly these values.
OCCUPYING_STATUSES: tuple[AppointmentStatus, ...] = (
    AppointmentStatus.HELD,
    AppointmentStatus.CONFIRMED,
    AppointmentStatus.COMPLETED,
)


class SummaryType(StrEnum):
    PRE_VISIT = "pre_visit"
    POST_VISIT = "post_visit"


class SummaryStatus(StrEnum):
    """An LLM summary is generated outside the request that triggered it.

    `PENDING` is therefore a normal state, not an error: the appointment exists and is valid
    while the model has not answered yet.
    """

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class UrgencyLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class NotificationType(StrEnum):
    BOOKING_CONFIRMATION = "booking_confirmation"
    APPOINTMENT_REMINDER = "appointment_reminder"
    CANCELLATION = "cancellation"
    LEAVE_CONFLICT = "leave_conflict"
    MEDICATION_REMINDER = "medication_reminder"


class NotificationStatus(StrEnum):
    """Delivery state of one outbox row.

    `FAILED` means the retry budget is exhausted and a human should look at it — the row is
    kept rather than deleted so the admin portal can surface it.
    """

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
