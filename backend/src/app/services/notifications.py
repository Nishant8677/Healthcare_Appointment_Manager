"""The notification outbox: queueing messages and rendering them.

Nothing here sends anything. Every function adds rows to the caller's session and leaves the
commit to them — that is the whole point. A booking and its confirmation emails commit
together or not at all, so the system can never be in the state where an appointment exists
but nobody was told, or where a patient is told about a booking that rolled back.

Payloads are denormalised on purpose: a queued message carries everything its template needs,
so rendering an email hours later cannot depend on rows that have since changed.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import Appointment
from app.models.doctor import DoctorProfile
from app.models.enums import NotificationStatus, NotificationType
from app.models.notification import NotificationJob
from app.models.user import User
from app.services.email import EmailMessage

logger = logging.getLogger(__name__)

HUMAN_TIME_FORMAT = "%A %d %B %Y at %H:%M"


def format_local(moment: datetime, zone: ZoneInfo) -> str:
    """A time a patient can read, in the clinic's own timezone."""
    return moment.astimezone(zone).strftime(HUMAN_TIME_FORMAT)


def _appointment_payload(
    *,
    appointment: Appointment,
    patient: User,
    doctor: DoctorProfile,
    zone: ZoneInfo,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "appointment_id": str(appointment.id),
        "patient_name": patient.full_name,
        "doctor_name": doctor.user.full_name,
        "specialisation": doctor.specialisation,
        "starts_at": appointment.starts_at.isoformat(),
        # Pre-formatted here so the template never needs the timezone at send time.
        "starts_at_local": format_local(appointment.starts_at, zone),
        "timezone": str(zone),
    }
    if extra:
        payload.update(extra)
    return payload


def enqueue(
    session: AsyncSession,
    *,
    notification_type: NotificationType,
    recipient: User,
    payload: dict[str, Any],
    scheduled_for: datetime,
    appointment_id: uuid.UUID | None = None,
    for_doctor: bool = False,
) -> NotificationJob:
    """Add one message to the outbox. The caller commits.

    `for_doctor` decides whose name the template puts in "your appointment with ...". Without
    it the doctor's copy names the doctor to themselves.
    """
    job = NotificationJob(
        notification_type=notification_type,
        status=NotificationStatus.PENDING,
        recipient_email=recipient.email,
        recipient_user_id=recipient.id,
        appointment_id=appointment_id,
        payload={
            **payload,
            "recipient_name": recipient.full_name,
            "recipient_is_doctor": for_doctor,
        },
        scheduled_for=scheduled_for,
    )
    session.add(job)
    return job


def enqueue_booking_confirmation(
    session: AsyncSession,
    *,
    appointment: Appointment,
    patient: User,
    doctor: DoctorProfile,
    zone: ZoneInfo,
    reminder_lead_hours: int,
    now: datetime | None = None,
) -> None:
    """Confirm to both sides, and queue the patient's reminder at the same time.

    The reminder is queued now rather than by a later job because its due time is already
    known — `scheduled_for` keeps it dormant until then, and one mechanism covers both.
    """
    reference = now or datetime.now(UTC)
    payload = _appointment_payload(
        appointment=appointment, patient=patient, doctor=doctor, zone=zone
    )

    enqueue(
        session,
        notification_type=NotificationType.BOOKING_CONFIRMATION,
        recipient=patient,
        payload=payload,
        scheduled_for=reference,
        appointment_id=appointment.id,
    )
    enqueue(
        session,
        notification_type=NotificationType.BOOKING_CONFIRMATION,
        recipient=doctor.user,
        payload=payload,
        scheduled_for=reference,
        appointment_id=appointment.id,
        for_doctor=True,
    )

    # If the appointment is sooner than the lead time, remind as soon as the worker runs
    # rather than skipping the reminder entirely.
    reminder_due = max(reference, appointment.starts_at - timedelta(hours=reminder_lead_hours))
    if reminder_due < appointment.starts_at:
        enqueue(
            session,
            notification_type=NotificationType.APPOINTMENT_REMINDER,
            recipient=patient,
            payload=payload,
            scheduled_for=reminder_due,
            appointment_id=appointment.id,
        )


def enqueue_cancellation(
    session: AsyncSession,
    *,
    appointment: Appointment,
    patient: User,
    doctor: DoctorProfile,
    zone: ZoneInfo,
    cancelled_by_clinic: bool,
    reason: str | None = None,
    now: datetime | None = None,
) -> None:
    """Tell both sides an appointment is off.

    `cancelled_by_clinic` changes the wording: a patient who cancelled their own appointment
    should not receive a message apologising to them for it.
    """
    reference = now or datetime.now(UTC)
    payload = _appointment_payload(
        appointment=appointment,
        patient=patient,
        doctor=doctor,
        zone=zone,
        extra={"cancelled_by_clinic": cancelled_by_clinic, "reason": reason},
    )

    for recipient, is_doctor in ((patient, False), (doctor.user, True)):
        enqueue(
            session,
            notification_type=NotificationType.CANCELLATION,
            recipient=recipient,
            payload=payload,
            scheduled_for=reference,
            appointment_id=appointment.id,
            for_doctor=is_doctor,
        )


def enqueue_leave_conflict(
    session: AsyncSession,
    *,
    appointment: Appointment,
    patient: User,
    doctor: DoctorProfile,
    zone: ZoneInfo,
    now: datetime | None = None,
) -> None:
    """Tell a patient their appointment is off because the doctor is away.

    Only the patient is told. The doctor is the one who booked the leave, so a message per
    cancelled appointment would be a stack of notifications about something they just did.
    """
    reference = now or datetime.now(UTC)
    payload = _appointment_payload(
        appointment=appointment,
        patient=patient,
        doctor=doctor,
        zone=zone,
        # Carried so the message can point at rebooking with the same doctor.
        extra={"doctor_profile_id": str(doctor.id)},
    )

    enqueue(
        session,
        notification_type=NotificationType.LEAVE_CONFLICT,
        recipient=patient,
        payload=payload,
        scheduled_for=reference,
        appointment_id=appointment.id,
    )


async def drop_pending_reminders(session: AsyncSession, appointment_id: uuid.UUID) -> None:
    """Remove reminders for an appointment that is no longer happening.

    Whether to send is a question about *current* state, unlike rendering, so it cannot be
    answered from the frozen payload. Deleting the undelivered rows is simpler and more
    obvious than teaching the worker to re-check every appointment before sending.
    """
    await session.execute(
        delete(NotificationJob).where(
            NotificationJob.appointment_id == appointment_id,
            NotificationJob.notification_type == NotificationType.APPOINTMENT_REMINDER,
            NotificationJob.status == NotificationStatus.PENDING,
        )
    )


async def pending_jobs_for(
    session: AsyncSession, appointment_id: uuid.UUID
) -> list[NotificationJob]:
    """Undelivered messages raised by one appointment. Used by tests and the admin view."""
    result = await session.execute(
        select(NotificationJob)
        .where(
            NotificationJob.appointment_id == appointment_id,
            NotificationJob.status == NotificationStatus.PENDING,
        )
        .order_by(NotificationJob.scheduled_for)
    )
    return list(result.scalars().all())


# --------------------------------------------------------------------------- rendering


def render(job: NotificationJob) -> EmailMessage:
    """Turn a queued job into the email to send.

    Reads only `job.payload`, never the database: a message must render identically whenever
    it is retried, including after the appointment it describes has changed.
    """
    payload = job.payload
    recipient_name = str(payload.get("recipient_name", "there"))
    builder = _TEMPLATES.get(job.notification_type, _render_generic)
    subject, body = builder(payload, recipient_name)

    return EmailMessage(
        to_address=job.recipient_email,
        to_name=recipient_name,
        subject=subject,
        body=body,
    )


def _appointment_line(payload: dict[str, Any]) -> str:
    """Describe the appointment from the recipient's side.

    A doctor reading "your appointment with Dr Asha Rao" when they *are* Dr Asha Rao is the
    kind of detail that makes an otherwise correct system look unfinished.
    """
    when = payload.get("starts_at_local", "the scheduled time")

    if payload.get("recipient_is_doctor"):
        return f"{when} with {payload.get('patient_name', 'your patient')}"

    return (
        f"{when} with {payload.get('doctor_name', 'your doctor')} "
        f"({payload.get('specialisation', 'consultation')})"
    )


def _render_confirmation(payload: dict[str, Any], recipient_name: str) -> tuple[str, str]:
    return (
        f"Appointment confirmed: {payload.get('starts_at_local', '')}".strip(),
        f"Hello {recipient_name},\n\n"
        f"Your appointment is confirmed for {_appointment_line(payload)}.\n\n"
        f"Times are shown in {payload.get('timezone', 'the clinic timezone')}.\n"
        "If you can no longer attend, please cancel so the slot can be offered to "
        "someone else.\n\n"
        "The Clinic",
    )


def _render_reminder(payload: dict[str, Any], recipient_name: str) -> tuple[str, str]:
    return (
        f"Reminder: appointment {payload.get('starts_at_local', 'soon')}",
        f"Hello {recipient_name},\n\n"
        f"This is a reminder of your appointment: {_appointment_line(payload)}.\n\n"
        "If you can no longer attend, please cancel so the slot can be reused.\n\n"
        "The Clinic",
    )


def _render_cancellation(payload: dict[str, Any], recipient_name: str) -> tuple[str, str]:
    by_clinic = bool(payload.get("cancelled_by_clinic"))
    reason = payload.get("reason")

    if by_clinic:
        opening = (
            "We are sorry to say that the clinic has had to cancel this appointment:\n\n"
            f"  {_appointment_line(payload)}\n\n"
            "Please book another time at your convenience."
        )
    else:
        opening = f"This appointment has been cancelled:\n\n  {_appointment_line(payload)}"

    reason_line = f"\n\nReason: {reason}" if reason else ""

    return (
        f"Appointment cancelled: {payload.get('starts_at_local', '')}".strip(),
        f"Hello {recipient_name},\n\n{opening}{reason_line}\n\nThe Clinic",
    )


def _render_leave_conflict(payload: dict[str, Any], recipient_name: str) -> tuple[str, str]:
    return (
        "Your appointment needs rebooking",
        f"Hello {recipient_name},\n\n"
        f"{payload.get('doctor_name', 'Your doctor')} is unavailable on the day of your "
        f"appointment ({payload.get('starts_at_local', '')}), so it has been cancelled.\n\n"
        "We are sorry for the disruption. Please book another time that suits you — "
        f"{payload.get('doctor_name', 'they')} is available on other days, and other "
        f"{payload.get('specialisation', 'clinic')} doctors may have earlier openings.\n\n"
        "The Clinic",
    )


def _render_medication_reminder(payload: dict[str, Any], recipient_name: str) -> tuple[str, str]:
    return (
        "Medication reminder",
        f"Hello {recipient_name},\n\n"
        f"This is your reminder to take {payload.get('drug_name', 'your medication')} "
        f"({payload.get('dosage', 'as prescribed')}).\n\n"
        "The Clinic",
    )


def _render_generic(payload: dict[str, Any], recipient_name: str) -> tuple[str, str]:
    return (
        "A message from the clinic",
        f"Hello {recipient_name},\n\nPlease contact the clinic for details.\n\nThe Clinic",
    )


_TEMPLATES = {
    NotificationType.BOOKING_CONFIRMATION: _render_confirmation,
    NotificationType.APPOINTMENT_REMINDER: _render_reminder,
    NotificationType.CANCELLATION: _render_cancellation,
    NotificationType.LEAVE_CONFLICT: _render_leave_conflict,
    NotificationType.MEDICATION_REMINDER: _render_medication_reminder,
}
