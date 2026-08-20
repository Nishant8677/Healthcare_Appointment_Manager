"""Booking: holding a slot, confirming it, cancelling and rescheduling.

Two different races need two different mechanisms, and conflating them is the usual way
double-booking protection ends up looking correct while being wrong:

* **Creating a hold** cannot be protected by `SELECT ... FOR UPDATE`, because the row being
  contended does not exist yet — there is nothing to lock. That race is decided by the partial
  unique index on `(doctor_profile_id, starts_at)`: concurrent inserts all reach the database,
  exactly one commits, and the rest raise `IntegrityError`, which becomes a clean 409. The
  availability check beforehand is a courtesy that produces a better error most of the time;
  it is explicitly *not* the guarantee.

* **Confirming, cancelling or rescheduling** operate on a row that already exists, so those
  take a real row lock and re-read the state inside the transaction. Without it, two
  simultaneous confirms of one hold could both pass their status check before either wrote.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.core.exceptions import (
    ActiveHoldExists,
    AppointmentNotCancellable,
    AppointmentNotConfirmable,
    AppointmentNotFound,
    HoldExpired,
    SlotTaken,
    SlotUnavailable,
)
from app.models.appointment import Appointment, SymptomReport
from app.models.doctor import DoctorProfile
from app.models.enums import AppointmentStatus, SummaryType, UserRole
from app.models.user import User
from app.services import calendar_sync, notifications, summaries
from app.services.availability import available_slots
from app.services.doctor_service import get_doctor

logger = logging.getLogger(__name__)

CANCELLABLE_STATUSES = (AppointmentStatus.HELD, AppointmentStatus.CONFIRMED)


# --------------------------------------------------------------------------- loading


def _appointment_relations() -> tuple[object, ...]:
    return (
        selectinload(Appointment.patient),
        selectinload(Appointment.doctor_profile).selectinload(DoctorProfile.user),
        selectinload(Appointment.symptom_report),
    )


async def load_appointment(session: AsyncSession, appointment_id: uuid.UUID) -> Appointment:
    """Load an appointment with everything a response needs."""
    result = await session.execute(
        select(Appointment)
        .options(*_appointment_relations())  # type: ignore[arg-type]
        .where(Appointment.id == appointment_id)
    )
    appointment = result.scalar_one_or_none()
    if appointment is None:
        raise AppointmentNotFound(str(appointment_id))
    return appointment


async def _lock_appointment(session: AsyncSession, appointment_id: uuid.UUID) -> Appointment:
    """Take a row lock on the appointment for the rest of the transaction.

    Loaded without eager relations on purpose: `FOR UPDATE` alongside an outer join is
    rejected by Postgres, so relations are re-loaded after the write instead.
    """
    result = await session.execute(
        select(Appointment).where(Appointment.id == appointment_id).with_for_update()
    )
    appointment = result.scalar_one_or_none()
    if appointment is None:
        raise AppointmentNotFound(str(appointment_id))
    return appointment


async def _doctor_profile_id_for(session: AsyncSession, user: User) -> uuid.UUID | None:
    result = await session.execute(select(DoctorProfile.id).where(DoctorProfile.user_id == user.id))
    return result.scalar_one_or_none()


# --------------------------------------------------------------------------- holding


async def _reject_if_already_holding(
    session: AsyncSession, patient_id: uuid.UUID, now: datetime
) -> None:
    """One live hold per patient.

    Without this, a patient could reserve every slot in a doctor's day and release none of
    them — denial of service by way of the booking form.
    """
    result = await session.execute(
        select(Appointment.id).where(
            Appointment.patient_id == patient_id,
            Appointment.status == AppointmentStatus.HELD,
            Appointment.hold_expires_at > now,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        raise ActiveHoldExists(str(existing))


async def _reclaim_expired_hold(
    session: AsyncSession, doctor_id: uuid.UUID, starts_at: datetime, now: datetime
) -> None:
    """Remove a lapsed hold occupying this slot.

    The availability query already treats an expired hold as free, but the unique index does
    not — its predicate matches on status alone. So the stale row has to go before the insert,
    or an abandoned hold would block the slot permanently. Deleted rather than kept as a
    tombstone: a hold that was never confirmed is a transient reservation, not clinical
    history, and it carries no symptom form.
    """
    await session.execute(
        delete(Appointment).where(
            Appointment.doctor_profile_id == doctor_id,
            Appointment.starts_at == starts_at,
            Appointment.status == AppointmentStatus.HELD,
            Appointment.hold_expires_at <= now,
        )
    )


async def hold_slot(
    session: AsyncSession,
    *,
    patient: User,
    doctor_id: uuid.UUID,
    starts_at: datetime,
    settings: Settings,
    now: datetime | None = None,
) -> Appointment:
    """Reserve a slot for the patient while they complete the symptom form.

    Raises:
        DoctorNotFound, SlotUnavailable, ActiveHoldExists, SlotTaken.
    """
    reference = now or datetime.now(UTC)

    if starts_at.tzinfo is None:
        raise SlotUnavailable("starts_at must include a timezone offset.")

    doctor = await get_doctor(session, doctor_id)
    if not doctor.user.is_active:
        raise SlotUnavailable("This doctor is not currently accepting appointments.")

    horizon = reference + timedelta(days=settings.booking_horizon_days)
    if starts_at > horizon:
        raise SlotUnavailable(
            f"Appointments can only be booked up to {settings.booking_horizon_days} days ahead."
        )

    zone = settings.clinic_zone
    local_day = starts_at.astimezone(zone).date()
    offered = await available_slots(session, doctor, local_day, zone=zone, now=reference)
    slot = next((candidate for candidate in offered if candidate.starts_at == starts_at), None)
    if slot is None:
        raise SlotUnavailable("That time is not an available appointment slot for this doctor.")

    await _reject_if_already_holding(session, patient.id, reference)
    await _reclaim_expired_hold(session, doctor.id, slot.starts_at, reference)

    appointment = Appointment(
        patient_id=patient.id,
        doctor_profile_id=doctor.id,
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        status=AppointmentStatus.HELD,
        hold_expires_at=reference + timedelta(minutes=settings.slot_hold_minutes),
    )
    session.add(appointment)

    try:
        await session.commit()
    except IntegrityError as exc:
        # The unique index rejected it: another patient committed this slot first. This is the
        # actual double-booking guarantee, not the availability check above.
        await session.rollback()
        raise SlotTaken("That slot was just taken by another patient.") from exc

    logger.info(
        "slot held",
        extra={"appointment_id": str(appointment.id), "doctor_profile_id": str(doctor.id)},
    )
    return await load_appointment(session, appointment.id)


# --------------------------------------------------------------------------- confirming


async def confirm_hold(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    patient: User,
    symptoms: str,
    settings: Settings,
    duration_days: int | None = None,
    additional_notes: str | None = None,
    now: datetime | None = None,
) -> Appointment:
    """Turn a live hold into a confirmed appointment, recording the symptom form.

    Raises:
        AppointmentNotFound, AppointmentNotConfirmable, HoldExpired.
    """
    reference = now or datetime.now(UTC)
    appointment = await _lock_appointment(session, appointment_id)

    # Someone else's appointment is reported as missing rather than forbidden, so the response
    # cannot be used to discover which appointment ids exist.
    if appointment.patient_id != patient.id:
        raise AppointmentNotFound(str(appointment_id))

    if appointment.status is not AppointmentStatus.HELD:
        raise AppointmentNotConfirmable(
            f"This appointment is {appointment.status.value} and cannot be confirmed."
        )

    if appointment.hold_expires_at is None or appointment.hold_expires_at <= reference:
        raise HoldExpired("The hold on this slot has expired. Please choose a slot again.")

    session.add(
        SymptomReport(
            appointment_id=appointment.id,
            symptoms=symptoms,
            duration_days=duration_days,
            additional_notes=additional_notes,
        )
    )
    appointment.status = AppointmentStatus.CONFIRMED
    appointment.hold_expires_at = None

    # Queued inside this transaction, not sent from it. If the commit below fails, the
    # confirmation emails disappear with the confirmation itself.
    # Requested inside this transaction, generated in the background. A slow or offline
    # model delays the doctor's brief; it never blocks the patient's booking.
    summaries.queue_summary(
        session,
        appointment_id=appointment.id,
        summary_type=SummaryType.PRE_VISIT,
        model=settings.llm_model,
    )

    doctor = await get_doctor(session, appointment.doctor_profile_id)
    notifications.enqueue_booking_confirmation(
        session,
        appointment=appointment,
        patient=patient,
        doctor=doctor,
        zone=settings.clinic_zone,
        reminder_lead_hours=settings.reminder_lead_hours,
        now=reference,
    )
    # Recorded in this transaction, written to Google by the worker afterwards. An outage at
    # Google delays a calendar entry; it never fails a booking.
    await calendar_sync.enqueue_appointment(
        session,
        appointment=appointment,
        patient=patient,
        doctor=doctor,
        zone=settings.clinic_zone,
    )

    await session.commit()
    logger.info("appointment confirmed", extra={"appointment_id": str(appointment.id)})
    return await load_appointment(session, appointment.id)


# --------------------------------------------------------------------------- cancelling


async def cancel_appointment(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    actor: User,
    settings: Settings,
    reason: str | None = None,
    now: datetime | None = None,
) -> Appointment:
    """Cancel an appointment.

    Who cancelled matters: a patient cancelling and the clinic cancelling are recorded as
    different statuses, because Phase 5 sends a different message for each.
    """
    reference = now or datetime.now(UTC)
    appointment = await _lock_appointment(session, appointment_id)

    if actor.role is UserRole.PATIENT:
        if appointment.patient_id != actor.id:
            raise AppointmentNotFound(str(appointment_id))
        new_status = AppointmentStatus.CANCELLED_BY_PATIENT
    elif actor.role is UserRole.DOCTOR:
        own_profile_id = await _doctor_profile_id_for(session, actor)
        if own_profile_id is None or appointment.doctor_profile_id != own_profile_id:
            raise AppointmentNotFound(str(appointment_id))
        new_status = AppointmentStatus.CANCELLED_BY_CLINIC
    else:
        new_status = AppointmentStatus.CANCELLED_BY_CLINIC

    if appointment.status not in CANCELLABLE_STATUSES:
        raise AppointmentNotCancellable(f"This appointment is already {appointment.status.value}.")

    appointment.status = new_status
    appointment.cancelled_at = reference
    appointment.cancellation_reason = reason

    # A reminder for an appointment that is no longer happening must not go out. Whether to
    # send depends on current state, which the frozen payload cannot answer, so the
    # undelivered rows are removed instead.
    await notifications.drop_pending_reminders(session, appointment.id)

    patient = await session.get(User, appointment.patient_id)
    doctor = await get_doctor(session, appointment.doctor_profile_id)
    if patient is not None:
        notifications.enqueue_cancellation(
            session,
            appointment=appointment,
            patient=patient,
            doctor=doctor,
            zone=settings.clinic_zone,
            cancelled_by_clinic=new_status is AppointmentStatus.CANCELLED_BY_CLINIC,
            reason=reason,
            now=reference,
        )

    # The calendar entries are marked for removal rather than deleted here, so a cancellation
    # never waits on Google and never fails because Google is down.
    await calendar_sync.enqueue_removal(session, appointment.id)

    await session.commit()
    logger.info(
        "appointment cancelled",
        extra={"appointment_id": str(appointment.id), "status": new_status.value},
    )
    return await load_appointment(session, appointment.id)


# --------------------------------------------------------------------------- rescheduling


async def reschedule_appointment(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    patient: User,
    new_starts_at: datetime,
    settings: Settings,
    now: datetime | None = None,
) -> Appointment:
    """Move a confirmed appointment to a different slot with the same doctor.

    The old booking is cancelled and a new confirmed one created in a single transaction, so
    a patient can never end up holding both or neither. The symptom form is carried across —
    the complaint has not changed just because the time did.

    Raises:
        AppointmentNotFound, AppointmentNotConfirmable, SlotUnavailable, SlotTaken.
    """
    reference = now or datetime.now(UTC)

    if new_starts_at.tzinfo is None:
        raise SlotUnavailable("starts_at must include a timezone offset.")

    original = await _lock_appointment(session, appointment_id)
    if original.patient_id != patient.id:
        raise AppointmentNotFound(str(appointment_id))
    if original.status is not AppointmentStatus.CONFIRMED:
        raise AppointmentNotConfirmable(
            f"Only a confirmed appointment can be rescheduled; this one is {original.status.value}."
        )
    if new_starts_at == original.starts_at:
        raise SlotUnavailable("That is the appointment's current time.")

    doctor = await get_doctor(session, original.doctor_profile_id)

    horizon = reference + timedelta(days=settings.booking_horizon_days)
    if new_starts_at > horizon:
        raise SlotUnavailable(
            f"Appointments can only be booked up to {settings.booking_horizon_days} days ahead."
        )

    zone = settings.clinic_zone
    offered = await available_slots(
        session, doctor, new_starts_at.astimezone(zone).date(), zone=zone, now=reference
    )
    slot = next((candidate for candidate in offered if candidate.starts_at == new_starts_at), None)
    if slot is None:
        raise SlotUnavailable("That time is not an available appointment slot for this doctor.")

    report_result = await session.execute(
        select(SymptomReport).where(SymptomReport.appointment_id == original.id)
    )
    report = report_result.scalar_one_or_none()

    replacement = Appointment(
        patient_id=original.patient_id,
        doctor_profile_id=original.doctor_profile_id,
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        status=AppointmentStatus.CONFIRMED,
    )
    session.add(replacement)

    original.status = AppointmentStatus.CANCELLED_BY_PATIENT
    original.cancelled_at = reference
    original.cancellation_reason = "Rescheduled by patient"

    if report is not None:
        session.add(
            SymptomReport(
                appointment=replacement,
                symptoms=report.symptoms,
                duration_days=report.duration_days,
                additional_notes=report.additional_notes,
            )
        )

    # Flush so the replacement has an id the notification rows can reference.
    await session.flush()
    await notifications.drop_pending_reminders(session, original.id)
    notifications.enqueue_cancellation(
        session,
        appointment=original,
        patient=patient,
        doctor=doctor,
        zone=settings.clinic_zone,
        cancelled_by_clinic=False,
        reason=original.cancellation_reason,
        now=reference,
    )
    notifications.enqueue_booking_confirmation(
        session,
        appointment=replacement,
        patient=patient,
        doctor=doctor,
        zone=settings.clinic_zone,
        reminder_lead_hours=settings.reminder_lead_hours,
        now=reference,
    )

    # Moves the existing calendar entries onto the replacement rather than deleting and
    # recreating them, so the entry in each participant's calendar shifts to the new time.
    await calendar_sync.transfer_on_reschedule(
        session,
        original_id=original.id,
        replacement=replacement,
        patient=patient,
        doctor=doctor,
        zone=settings.clinic_zone,
    )

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise SlotTaken("That slot was just taken by another patient.") from exc

    logger.info(
        "appointment rescheduled",
        extra={"from_appointment": str(original.id), "to_appointment": str(replacement.id)},
    )
    return await load_appointment(session, replacement.id)


# --------------------------------------------------------------------------- listing


async def list_appointments(
    session: AsyncSession,
    *,
    user: User,
    include_cancelled: bool = False,
    now: datetime | None = None,
) -> Sequence[Appointment]:
    """Appointments visible to this user, soonest first.

    Scoped by role in the query itself: a patient's own bookings, a doctor's own schedule,
    everything for an admin.
    """
    reference = now or datetime.now(UTC)
    query = select(Appointment).options(*_appointment_relations())  # type: ignore[arg-type]

    if user.role is UserRole.PATIENT:
        query = query.where(Appointment.patient_id == user.id)
    elif user.role is UserRole.DOCTOR:
        own_profile_id = await _doctor_profile_id_for(session, user)
        if own_profile_id is None:
            return []
        query = query.where(Appointment.doctor_profile_id == own_profile_id)

    if not include_cancelled:
        # An expired hold is excluded too: it is not a booking, it is an abandoned attempt.
        query = query.where(
            or_(
                Appointment.status.in_([AppointmentStatus.CONFIRMED, AppointmentStatus.COMPLETED]),
                (Appointment.status == AppointmentStatus.HELD)
                & (Appointment.hold_expires_at > reference),
            )
        )

    result = await session.execute(query.order_by(Appointment.starts_at))
    return result.scalars().all()
