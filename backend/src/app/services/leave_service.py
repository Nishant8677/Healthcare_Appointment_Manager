"""Recording doctor leave, and dealing with the patients already booked that day.

This is where the previous two phases meet: one admin action has to cancel several
appointments and reliably tell each of those patients, without any of it half-happening. All
of it — the leave day, every cancellation, every queued message — is a single transaction, so
the outcome is either the complete cascade or nothing at all.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.core.exceptions import (
    DuplicateLeaveDay,
    InvalidSchedule,
    LeaveConflictsExist,
    LeaveDayNotFound,
)
from app.models.appointment import Appointment
from app.models.doctor import DoctorLeaveDay, DoctorProfile
from app.models.enums import AppointmentStatus
from app.services import notifications
from app.services.doctor_service import get_doctor

logger = logging.getLogger(__name__)

# Statuses that a leave day has to clear out of the way.
BLOCKING_STATUSES = (AppointmentStatus.HELD, AppointmentStatus.CONFIRMED)


@dataclass(frozen=True, slots=True)
class LeaveOutcome:
    """What recording the leave actually did."""

    leave_day: DoctorLeaveDay
    cancelled: int
    patients_notified: int


async def appointments_on(
    session: AsyncSession,
    doctor_id: uuid.UUID,
    day: date,
    *,
    zone: ZoneInfo,
) -> Sequence[Appointment]:
    """Live appointments falling on a calendar date in the clinic's timezone.

    Appointments are stored as UTC instants, so "that day" is a local-calendar question. The
    query brackets a generous UTC window and the exact local date is then matched in Python —
    simpler to read than composing the boundary in SQL, and immune to a midnight that does not
    exist on a daylight-saving day.
    """
    window_start = datetime.combine(day - timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    window_end = datetime.combine(day + timedelta(days=2), time.min, tzinfo=zone).astimezone(UTC)

    result = await session.execute(
        select(Appointment)
        .options(
            selectinload(Appointment.patient),
            selectinload(Appointment.doctor_profile).selectinload(DoctorProfile.user),
        )
        .where(
            Appointment.doctor_profile_id == doctor_id,
            Appointment.starts_at >= window_start,
            Appointment.starts_at < window_end,
            Appointment.status.in_(BLOCKING_STATUSES),
        )
        .order_by(Appointment.starts_at)
    )

    return [
        appointment
        for appointment in result.scalars().all()
        if appointment.starts_at.astimezone(zone).date() == day
    ]


async def record_leave(
    session: AsyncSession,
    doctor_id: uuid.UUID,
    *,
    leave_date: date,
    settings: Settings,
    reason: str | None = None,
    cancel_existing_appointments: bool = False,
    now: datetime | None = None,
    today: date | None = None,
) -> LeaveOutcome:
    """Mark a doctor unavailable for a day, cancelling and notifying as needed.

    Refuses when appointments exist unless `cancel_existing_appointments` is set. Cancelling
    other people's medical appointments must be a deliberate act, never a side effect of
    recording a date.

    Raises:
        DoctorNotFound, InvalidSchedule, DuplicateLeaveDay, LeaveConflictsExist.
    """
    reference = now or datetime.now(UTC)
    reference_day = today or reference.astimezone(settings.clinic_zone).date()

    if leave_date < reference_day:
        raise InvalidSchedule(
            f"{leave_date.isoformat()} is in the past; leave can only be recorded "
            f"from {reference_day.isoformat()} onwards."
        )

    doctor = await get_doctor(session, doctor_id)
    zone = settings.clinic_zone
    affected = await appointments_on(session, doctor.id, leave_date, zone=zone)

    if affected and not cancel_existing_appointments:
        raise LeaveConflictsExist(len(affected))

    leave = DoctorLeaveDay(doctor_profile_id=doctor.id, leave_date=leave_date, reason=reason)
    session.add(leave)

    notified = 0
    for appointment in affected:
        was_confirmed = appointment.status is AppointmentStatus.CONFIRMED
        appointment.status = AppointmentStatus.CANCELLED_BY_CLINIC
        appointment.cancelled_at = reference
        appointment.cancellation_reason = (
            f"{doctor.user.full_name} is unavailable on {leave_date.isoformat()}"
        )

        await notifications.drop_pending_reminders(session, appointment.id)

        # A held slot was never a booking: the patient is still choosing, and will simply find
        # the slot gone. Emailing them about an appointment they never made would confuse.
        if was_confirmed and appointment.patient is not None:
            notifications.enqueue_leave_conflict(
                session,
                appointment=appointment,
                patient=appointment.patient,
                doctor=doctor,
                zone=zone,
                now=reference,
            )
            notified += 1

    try:
        await session.commit()
    except IntegrityError as exc:
        # The unique constraint on (doctor, date) is the arbiter, so two admins recording the
        # same leave concurrently cannot both run the cascade.
        await session.rollback()
        raise DuplicateLeaveDay(leave_date.isoformat()) from exc

    await session.refresh(leave)
    logger.info(
        "leave recorded",
        extra={
            "doctor_profile_id": str(doctor.id),
            "leave_date": leave_date.isoformat(),
            "cancelled": len(affected),
            "patients_notified": notified,
        },
    )
    return LeaveOutcome(leave_day=leave, cancelled=len(affected), patients_notified=notified)


async def remove_leave_day(
    session: AsyncSession, doctor_id: uuid.UUID, leave_id: uuid.UUID
) -> None:
    """Delete a recorded leave day.

    Appointments cancelled when the leave was recorded are *not* restored: those patients have
    already been told it is off, and silently reinstating an appointment somebody believes is
    cancelled would be worse than making them rebook. The slots simply become available again.

    Raises:
        LeaveDayNotFound: no such leave day for this doctor.
    """
    result = await session.execute(
        select(DoctorLeaveDay).where(
            DoctorLeaveDay.id == leave_id,
            # Scoped to the doctor in the query, so a valid leave id belonging to another
            # doctor cannot be deleted through this route.
            DoctorLeaveDay.doctor_profile_id == doctor_id,
        )
    )
    leave = result.scalar_one_or_none()
    if leave is None:
        raise LeaveDayNotFound(str(leave_id))

    await session.delete(leave)
    await session.commit()
