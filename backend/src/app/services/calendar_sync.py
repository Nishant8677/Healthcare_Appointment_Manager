"""Recording what each participant's calendar should show.

Called from inside the transaction that changes an appointment, exactly like the notification
outbox: the desired calendar state is committed *with* the booking, so the two cannot
disagree. Nothing here talks to Google — that is the worker's job, and it happens afterwards
so a slow or unreachable Calendar API delays an event, never a booking.

The single idea worth carrying away: a row is a *goal*, not an instruction. Writing "this
appointment should not be on your calendar" over "this appointment should be on your calendar"
makes the two impossible to apply out of order, which is the failure a command queue has to
work to avoid and this one cannot express.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.appointment import Appointment
from app.models.calendar import CalendarConnection, CalendarSyncJob
from app.models.doctor import DoctorProfile
from app.models.enums import (
    AppointmentStatus,
    CalendarSyncAction,
    CalendarSyncStatus,
    UserRole,
)
from app.models.user import User
from app.services.google_calendar import CalendarEvent, derive_event_id

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- connections


async def get_connection(session: AsyncSession, user_id: uuid.UUID) -> CalendarConnection | None:
    """This user's calendar connection, connected or revoked."""
    result = await session.execute(
        select(CalendarConnection).where(CalendarConnection.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def active_connections(
    session: AsyncSession, user_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, CalendarConnection]:
    """The subset of those users who currently have a working calendar connection."""
    ids = [user_id for user_id in user_ids if user_id is not None]
    if not ids:
        return {}
    result = await session.execute(
        select(CalendarConnection).where(
            CalendarConnection.user_id.in_(ids),
            CalendarConnection.revoked_at.is_(None),
        )
    )
    return {connection.user_id: connection for connection in result.scalars().all()}


# --------------------------------------------------------------------------- event content


def build_payload(
    *,
    appointment: Appointment,
    patient: User,
    doctor: DoctorProfile,
    zone: ZoneInfo,
    for_doctor: bool,
) -> dict[str, Any]:
    """The event fields, frozen at enqueue time.

    Deliberately excludes the symptom report. A calendar entry syncs to phones, watches and
    whatever else the account is signed into, and is often visible to anyone glancing at a
    screen — it is the wrong place for a description of somebody's medical complaint. The
    time, the other party's name and a reference are enough to be useful.

    It also does not restate the start time in the description. The event already carries the
    instant, and Google renders it in whatever zone the reader's calendar is set to; a second
    copy formatted in the clinic's zone is only correct for readers who happen to share it.
    A patient in Chennai read "Starts: ... at 09:00" on an entry their calendar was drawing at
    2:30pm — both were right, which is exactly what makes a second source of truth harmful.
    """
    other_party = patient.full_name if for_doctor else f"Dr {doctor.user.full_name}"

    if for_doctor:
        summary = f"Consultation — {patient.full_name}"
        description = (
            f"Patient: {patient.full_name}\n"
            f"Contact: {patient.email}\n"
            f"Specialisation: {doctor.specialisation}\n\n"
            "Booked through the Healthcare Appointment Manager."
        )
    else:
        summary = f"Appointment with {other_party}"
        description = (
            f"Doctor: Dr {doctor.user.full_name}\n"
            f"Specialisation: {doctor.specialisation}\n\n"
            "Booked through the Healthcare Appointment Manager."
        )

    return {
        "summary": summary,
        "description": description,
        "starts_at": appointment.starts_at.isoformat(),
        "ends_at": appointment.ends_at.isoformat(),
        "time_zone": str(zone),
    }


def event_from_job(job: CalendarSyncJob) -> CalendarEvent:
    """Rebuild the event the worker should write from the frozen payload."""
    payload = job.payload
    return CalendarEvent(
        event_id=job.google_event_id,
        summary=str(payload["summary"]),
        description=str(payload["description"]),
        starts_at=datetime.fromisoformat(str(payload["starts_at"])),
        ends_at=datetime.fromisoformat(str(payload["ends_at"])),
        time_zone=str(payload["time_zone"]),
    )


# --------------------------------------------------------------------------- enqueueing


async def _upsert_job(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    user_id: uuid.UUID,
    action: CalendarSyncAction,
    payload: dict[str, Any],
    calendar_id: str,
) -> CalendarSyncJob:
    """Write the desired state for one calendar, replacing whatever was there.

    `synced_at` is deliberately preserved across the rewrite: it records whether Google holds
    the event, which stays true no matter how many times the desired state changes.
    """
    result = await session.execute(
        select(CalendarSyncJob).where(
            CalendarSyncJob.appointment_id == appointment_id,
            CalendarSyncJob.user_id == user_id,
        )
    )
    job = result.scalar_one_or_none()

    if job is None:
        job = CalendarSyncJob(
            appointment_id=appointment_id,
            user_id=user_id,
            action=action,
            status=CalendarSyncStatus.PENDING,
            google_event_id=derive_event_id(appointment_id, user_id),
            calendar_id=calendar_id,
            payload=payload,
        )
        session.add(job)
        return job

    job.action = action
    job.payload = payload
    job.calendar_id = calendar_id
    job.status = CalendarSyncStatus.PENDING
    # A fresh retry budget: the previous failures described a request that is no longer the
    # one being made.
    job.attempts = 0
    job.next_attempt_at = None
    job.last_error = None
    return job


async def enqueue_appointment(
    session: AsyncSession,
    *,
    appointment: Appointment,
    patient: User,
    doctor: DoctorProfile,
    zone: ZoneInfo,
) -> list[CalendarSyncJob]:
    """Queue calendar entries for whichever participants have connected a calendar.

    Nothing is written for a participant without a connection. That keeps the table empty
    for clinics that never set Google up — the common case — instead of accumulating a
    permanent trail of rows that could only ever be skipped.
    """
    participants = {patient.id: False, doctor.user_id: True}
    connections = await active_connections(session, participants)
    if not connections:
        return []

    jobs: list[CalendarSyncJob] = []
    for user_id, for_doctor in participants.items():
        connection = connections.get(user_id)
        if connection is None:
            continue
        jobs.append(
            await _upsert_job(
                session,
                appointment_id=appointment.id,
                user_id=user_id,
                action=CalendarSyncAction.SYNC,
                payload=build_payload(
                    appointment=appointment,
                    patient=patient,
                    doctor=doctor,
                    zone=zone,
                    for_doctor=for_doctor,
                ),
                calendar_id=connection.calendar_id,
            )
        )
    return jobs


async def enqueue_removal(session: AsyncSession, appointment_id: uuid.UUID) -> int:
    """Mark every calendar entry for this appointment for deletion.

    Rows are rewritten rather than deleted even when nothing was ever sent to Google. A row
    the worker has already claimed is mid-flight; deleting it would race the worker into
    creating an event for a cancelled appointment that nothing is left to remove.
    """
    result = await session.execute(
        select(CalendarSyncJob).where(CalendarSyncJob.appointment_id == appointment_id)
    )
    jobs = list(result.scalars().all())
    for job in jobs:
        job.action = CalendarSyncAction.DELETE
        job.status = CalendarSyncStatus.PENDING
        job.attempts = 0
        job.next_attempt_at = None
        job.last_error = None
    return len(jobs)


async def transfer_on_reschedule(
    session: AsyncSession,
    *,
    original_id: uuid.UUID,
    replacement: Appointment,
    patient: User,
    doctor: DoctorProfile,
    zone: ZoneInfo,
) -> None:
    """Move the original appointment's calendar entries onto its replacement.

    A reschedule creates a new appointment row and cancels the old one, so the obvious
    implementation is "delete the old event, create a new one". Carrying the existing event
    id across instead means the entry in the patient's calendar *moves* — the same way it
    would if they dragged it — rather than vanishing and reappearing, which on a phone reads
    as a cancellation followed by a surprise booking.
    """
    result = await session.execute(
        select(CalendarSyncJob).where(CalendarSyncJob.appointment_id == original_id)
    )
    existing = {job.user_id: job for job in result.scalars().all()}

    participants = {patient.id: False, doctor.user_id: True}
    connections = await active_connections(session, participants)

    for user_id, for_doctor in participants.items():
        connection = connections.get(user_id)
        if connection is None:
            continue
        payload = build_payload(
            appointment=replacement,
            patient=patient,
            doctor=doctor,
            zone=zone,
            for_doctor=for_doctor,
        )
        job = existing.get(user_id)
        if job is None:
            # Connected between the original booking and the reschedule: no event to move,
            # so this is an ordinary create.
            await _upsert_job(
                session,
                appointment_id=replacement.id,
                user_id=user_id,
                action=CalendarSyncAction.SYNC,
                payload=payload,
                calendar_id=connection.calendar_id,
            )
            continue

        # Re-point the row — and with it the event id and its synced state — at the new
        # appointment. The unique constraint is on (appointment_id, user_id), so this cannot
        # collide: the replacement is a brand-new appointment.
        job.appointment_id = replacement.id
        job.action = CalendarSyncAction.SYNC
        job.status = CalendarSyncStatus.PENDING
        job.payload = payload
        job.calendar_id = connection.calendar_id
        job.attempts = 0
        job.next_attempt_at = None
        job.last_error = None

    # Any entry belonging to a participant who has since disconnected stays on the original
    # appointment and is removed, rather than being left pointing at a cancelled booking.
    for user_id, job in existing.items():
        if user_id not in connections:
            job.action = CalendarSyncAction.DELETE
            job.status = CalendarSyncStatus.PENDING
            job.attempts = 0
            job.next_attempt_at = None


async def backfill_for_user(
    session: AsyncSession,
    *,
    user: User,
    connection: CalendarConnection,
    zone: ZoneInfo,
    limit: int,
    now: datetime | None = None,
) -> int:
    """Queue the user's upcoming appointments after they connect a calendar.

    Without this, connecting would only affect appointments booked from that moment on, and
    the feature would look broken to the first person who tries it: they connect, see nothing
    appear, and conclude it does not work. Bounded by `limit` so connecting can never queue
    an unbounded amount of work.
    """
    reference = now or datetime.now(UTC)
    query = (
        select(Appointment)
        .options(
            selectinload(Appointment.patient),
            selectinload(Appointment.doctor_profile).selectinload(DoctorProfile.user),
        )
        .where(
            Appointment.starts_at > reference,
            Appointment.status.in_((AppointmentStatus.CONFIRMED, AppointmentStatus.HELD)),
        )
        .order_by(Appointment.starts_at)
        .limit(limit)
    )

    if user.role is UserRole.DOCTOR:
        profile_result = await session.execute(
            select(DoctorProfile.id).where(DoctorProfile.user_id == user.id)
        )
        profile_id = profile_result.scalar_one_or_none()
        if profile_id is None:
            return 0
        query = query.where(Appointment.doctor_profile_id == profile_id)
    else:
        query = query.where(Appointment.patient_id == user.id)

    result = await session.execute(query)
    appointments: Sequence[Appointment] = result.scalars().all()

    for appointment in appointments:
        doctor = appointment.doctor_profile
        await _upsert_job(
            session,
            appointment_id=appointment.id,
            user_id=user.id,
            action=CalendarSyncAction.SYNC,
            payload=build_payload(
                appointment=appointment,
                patient=appointment.patient,
                doctor=doctor,
                zone=zone,
                for_doctor=user.id == doctor.user_id,
            ),
            calendar_id=connection.calendar_id,
        )

    if appointments:
        logger.info(
            "queued existing appointments for a newly connected calendar",
            extra={"user_id": str(user.id), "appointments": len(appointments)},
        )
    return len(appointments)
