"""The reconciler worker: how each way Google can fail is handled.

The gateway is a fake throughout, so every branch — a rate limit, a revoked grant, an event
the user deleted by hand — is reachable deterministically. What the tests are really checking
is the taxonomy: a transient failure must retry, a permanent one must stop, and a user who
simply has no calendar must not appear anywhere near an error count.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DoctorProfile, User
from app.models.appointment import Appointment
from app.models.calendar import CalendarConnection, CalendarSyncJob
from app.models.enums import AppointmentStatus, CalendarSyncAction, CalendarSyncStatus
from app.services.google_calendar import (
    CalendarEvent,
    CalendarPermanentError,
    CalendarTransientError,
    InMemoryCalendarGateway,
)
from app.services.google_oauth import GoogleAuthRevoked
from app.services.token_crypto import TokenCipher
from app.workers.calendar_worker import sync_once

MakeUser = Callable[..., Awaitable[User]]
MakeDoctor = Callable[..., Awaitable[DoctorProfile]]
ConnectCalendar = Callable[..., Awaitable[CalendarConnection]]


class FailingGateway:
    """A gateway that raises whatever the test asks for, and counts its calls."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def upsert_event(
        self, *, refresh_token: str, calendar_id: str, event: CalendarEvent, exists: bool
    ) -> None:
        self.calls += 1
        raise self.error

    async def delete_event(self, *, refresh_token: str, calendar_id: str, event_id: str) -> None:
        self.calls += 1
        raise self.error


async def make_appointment(
    session: AsyncSession, patient: User, doctor: DoctorProfile
) -> Appointment:
    starts = datetime.now(UTC) + timedelta(days=2)
    appointment = Appointment(
        patient_id=patient.id,
        doctor_profile_id=doctor.id,
        starts_at=starts,
        ends_at=starts + timedelta(minutes=30),
        status=AppointmentStatus.CONFIRMED,
    )
    session.add(appointment)
    await session.commit()
    await session.refresh(appointment)
    return appointment


async def make_job(
    session: AsyncSession,
    *,
    appointment: Appointment,
    user: User,
    action: CalendarSyncAction = CalendarSyncAction.SYNC,
    synced: bool = False,
) -> CalendarSyncJob:
    job = CalendarSyncJob(
        appointment_id=appointment.id,
        user_id=user.id,
        action=action,
        status=CalendarSyncStatus.PENDING,
        google_event_id=f"e{uuid.uuid4().hex[:20]}".replace("w", "v").lower(),
        calendar_id="primary",
        payload={
            "summary": "Appointment with Dr Test",
            "description": "Cardiology",
            "starts_at": appointment.starts_at.isoformat(),
            "ends_at": appointment.ends_at.isoformat(),
            "time_zone": "UTC",
        },
        synced_at=datetime.now(UTC) if synced else None,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


# --------------------------------------------------------------------------- happy paths


async def test_a_pending_job_writes_the_event_and_records_it(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    connect_calendar: ConnectCalendar,
    token_cipher: TokenCipher,
) -> None:
    patient = await make_user()
    doctor = await make_doctor()
    await connect_calendar(patient)
    appointment = await make_appointment(db_session, patient, doctor)
    job = await make_job(db_session, appointment=appointment, user=patient)
    gateway = InMemoryCalendarGateway()

    report = await sync_once(db_session, gateway, token_cipher)

    assert report.synced == 1
    assert ("primary", job.google_event_id) in gateway.events
    await db_session.refresh(job)
    assert job.status is CalendarSyncStatus.SYNCED
    assert job.synced_at is not None


async def test_a_delete_removes_the_event_and_forgets_it(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    connect_calendar: ConnectCalendar,
    token_cipher: TokenCipher,
) -> None:
    """`synced_at` is cleared so a later re-sync inserts rather than trying to update a
    calendar entry Google no longer holds."""
    patient = await make_user()
    doctor = await make_doctor()
    await connect_calendar(patient)
    appointment = await make_appointment(db_session, patient, doctor)
    job = await make_job(
        db_session,
        appointment=appointment,
        user=patient,
        action=CalendarSyncAction.DELETE,
        synced=True,
    )
    gateway = InMemoryCalendarGateway()

    report = await sync_once(db_session, gateway, token_cipher)

    assert report.deleted == 1
    assert gateway.deleted == [("primary", job.google_event_id)]
    await db_session.refresh(job)
    assert job.status is CalendarSyncStatus.SYNCED
    assert job.synced_at is None


async def test_deleting_an_event_that_was_never_created_makes_no_network_call(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    connect_calendar: ConnectCalendar,
    token_cipher: TokenCipher,
) -> None:
    """Booked and cancelled before the worker ever ran. The desired state already holds, so
    a round trip to Google would be pure waste — and would answer 404 anyway."""
    patient = await make_user()
    doctor = await make_doctor()
    await connect_calendar(patient)
    appointment = await make_appointment(db_session, patient, doctor)
    job = await make_job(
        db_session, appointment=appointment, user=patient, action=CalendarSyncAction.DELETE
    )
    gateway = InMemoryCalendarGateway()

    report = await sync_once(db_session, gateway, token_cipher)

    assert report.deleted == 1
    assert gateway.deleted == []
    await db_session.refresh(job)
    assert job.status is CalendarSyncStatus.SYNCED


async def test_an_already_synced_job_updates_rather_than_inserts(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    connect_calendar: ConnectCalendar,
    token_cipher: TokenCipher,
) -> None:
    seen: list[bool] = []

    class RecordingGateway(InMemoryCalendarGateway):
        async def upsert_event(
            self, *, refresh_token: str, calendar_id: str, event: CalendarEvent, exists: bool
        ) -> None:
            seen.append(exists)
            await super().upsert_event(
                refresh_token=refresh_token,
                calendar_id=calendar_id,
                event=event,
                exists=exists,
            )

    patient = await make_user()
    doctor = await make_doctor()
    await connect_calendar(patient)
    appointment = await make_appointment(db_session, patient, doctor)
    await make_job(db_session, appointment=appointment, user=patient, synced=True)

    await sync_once(db_session, RecordingGateway(), token_cipher)

    assert seen == [True]


# --------------------------------------------------------------------------- skipping


async def test_no_connection_is_skipped_not_failed(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    token_cipher: TokenCipher,
) -> None:
    """Most patients will never connect a calendar. That must never look like an incident."""
    patient = await make_user()
    doctor = await make_doctor()
    appointment = await make_appointment(db_session, patient, doctor)
    job = await make_job(db_session, appointment=appointment, user=patient)

    report = await sync_once(db_session, InMemoryCalendarGateway(), token_cipher)

    assert report == type(report)(skipped=1)
    await db_session.refresh(job)
    assert job.status is CalendarSyncStatus.SKIPPED
    assert job.attempts == 0


async def test_a_revoked_connection_is_skipped(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    connect_calendar: ConnectCalendar,
    token_cipher: TokenCipher,
) -> None:
    patient = await make_user()
    doctor = await make_doctor()
    await connect_calendar(patient, revoked=True)
    appointment = await make_appointment(db_session, patient, doctor)
    await make_job(db_session, appointment=appointment, user=patient)

    report = await sync_once(db_session, InMemoryCalendarGateway(), token_cipher)

    assert report.skipped == 1
    assert report.failed == 0


# --------------------------------------------------------------------------- failure modes


async def test_a_transient_failure_retries_with_backoff(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    connect_calendar: ConnectCalendar,
    token_cipher: TokenCipher,
) -> None:
    patient = await make_user()
    doctor = await make_doctor()
    await connect_calendar(patient)
    appointment = await make_appointment(db_session, patient, doctor)
    job = await make_job(db_session, appointment=appointment, user=patient)
    now = datetime.now(UTC)

    report = await sync_once(
        db_session, FailingGateway(CalendarTransientError("Google is down")), token_cipher, now=now
    )

    assert report.retried == 1
    await db_session.refresh(job)
    assert job.status is CalendarSyncStatus.PENDING
    assert job.attempts == 1
    assert job.next_attempt_at is not None
    assert job.next_attempt_at > now
    assert "Google is down" in (job.last_error or "")


async def test_a_transient_failure_stops_once_the_budget_is_spent(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    connect_calendar: ConnectCalendar,
    token_cipher: TokenCipher,
) -> None:
    patient = await make_user()
    doctor = await make_doctor()
    await connect_calendar(patient)
    appointment = await make_appointment(db_session, patient, doctor)
    job = await make_job(db_session, appointment=appointment, user=patient)
    job.attempts = 2
    await db_session.commit()

    report = await sync_once(
        db_session,
        FailingGateway(CalendarTransientError("still down")),
        token_cipher,
        max_attempts=3,
    )

    assert report.failed == 1
    await db_session.refresh(job)
    assert job.status is CalendarSyncStatus.FAILED


async def test_a_permanent_rejection_is_not_retried_even_once(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    connect_calendar: ConnectCalendar,
    token_cipher: TokenCipher,
) -> None:
    """Retrying a request Google will refuse identically only spends quota."""
    patient = await make_user()
    doctor = await make_doctor()
    await connect_calendar(patient)
    appointment = await make_appointment(db_session, patient, doctor)
    job = await make_job(db_session, appointment=appointment, user=patient)
    gateway = FailingGateway(CalendarPermanentError("invalid resource id"))

    report = await sync_once(db_session, gateway, token_cipher, max_attempts=5)

    assert report.failed == 1
    assert gateway.calls == 1
    await db_session.refresh(job)
    assert job.status is CalendarSyncStatus.FAILED
    assert job.attempts == 1


async def test_a_grant_revoked_at_google_marks_the_connection_dead(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    connect_calendar: ConnectCalendar,
    token_cipher: TokenCipher,
) -> None:
    """Without this the worker would keep calling Google for a user who has withdrawn
    access, once per poll, forever."""
    patient = await make_user()
    doctor = await make_doctor()
    connection = await connect_calendar(patient)
    appointment = await make_appointment(db_session, patient, doctor)
    await make_job(db_session, appointment=appointment, user=patient)

    report = await sync_once(
        db_session, FailingGateway(GoogleAuthRevoked("grant withdrawn")), token_cipher
    )

    assert report.skipped == 1
    await db_session.refresh(connection)
    assert connection.revoked_at is not None
    assert connection.is_active is False


async def test_an_unreadable_token_fails_the_job_and_asks_for_a_reconnect(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    connect_calendar: ConnectCalendar,
) -> None:
    """The encryption key was rotated. No retry recovers the old token, so the connection is
    marked so the portal can say "reconnect" instead of failing silently forever."""
    patient = await make_user()
    doctor = await make_doctor()
    connection = await connect_calendar(patient)
    appointment = await make_appointment(db_session, patient, doctor)
    job = await make_job(db_session, appointment=appointment, user=patient)
    wrong_key = TokenCipher("R3U9ZXrqxNnDSf46XXW3bGeIiCOHEZ7huU6rFMV4Rmc=")

    report = await sync_once(db_session, InMemoryCalendarGateway(), wrong_key)

    assert report.failed == 1
    await db_session.refresh(job)
    await db_session.refresh(connection)
    assert job.status is CalendarSyncStatus.FAILED
    assert connection.revoked_at is not None


async def test_an_unexpected_error_is_retried_rather_than_ending_the_pass(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    connect_calendar: ConnectCalendar,
    token_cipher: TokenCipher,
) -> None:
    patient = await make_user()
    doctor = await make_doctor()
    await connect_calendar(patient)
    appointment = await make_appointment(db_session, patient, doctor)
    job = await make_job(db_session, appointment=appointment, user=patient)

    report = await sync_once(
        db_session, FailingGateway(RuntimeError("a bug, not a Google problem")), token_cipher
    )

    assert report.retried == 1
    await db_session.refresh(job)
    assert "unexpected error" in (job.last_error or "")


# --------------------------------------------------------------------------- batching


async def test_one_calendar_failing_does_not_stop_the_others(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    connect_calendar: ConnectCalendar,
    token_cipher: TokenCipher,
) -> None:
    """Each job commits on its own, so a failure cannot roll back a neighbour's success."""
    doctor = await make_doctor()
    first = await make_user()
    second = await make_user()
    await connect_calendar(first)
    await connect_calendar(second)
    appointment = await make_appointment(db_session, first, doctor)
    await make_job(db_session, appointment=appointment, user=first)
    await make_job(db_session, appointment=appointment, user=second)

    calls = {"n": 0}

    class FlakyGateway(InMemoryCalendarGateway):
        async def upsert_event(
            self, *, refresh_token: str, calendar_id: str, event: CalendarEvent, exists: bool
        ) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise CalendarTransientError("first one fails")
            await super().upsert_event(
                refresh_token=refresh_token,
                calendar_id=calendar_id,
                event=event,
                exists=exists,
            )

    report = await sync_once(db_session, FlakyGateway(), token_cipher)

    assert report.retried == 1
    assert report.synced == 1


async def test_a_job_waiting_out_its_backoff_is_left_alone(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    connect_calendar: ConnectCalendar,
    token_cipher: TokenCipher,
) -> None:
    patient = await make_user()
    doctor = await make_doctor()
    await connect_calendar(patient)
    appointment = await make_appointment(db_session, patient, doctor)
    job = await make_job(db_session, appointment=appointment, user=patient)
    job.next_attempt_at = datetime.now(UTC) + timedelta(minutes=10)
    await db_session.commit()

    report = await sync_once(db_session, InMemoryCalendarGateway(), token_cipher)

    assert report.processed == 0


async def test_the_pass_respects_its_limit(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    connect_calendar: ConnectCalendar,
    token_cipher: TokenCipher,
) -> None:
    doctor = await make_doctor()
    users = [await make_user() for _ in range(3)]
    for user in users:
        await connect_calendar(user)
    appointment = await make_appointment(db_session, users[0], doctor)
    for user in users:
        await make_job(db_session, appointment=appointment, user=user)

    report = await sync_once(db_session, InMemoryCalendarGateway(), token_cipher, limit=2)

    assert report.processed == 2


async def test_no_work_is_a_no_op(db_session: AsyncSession, token_cipher: TokenCipher) -> None:
    assert (await sync_once(db_session, InMemoryCalendarGateway(), token_cipher)).processed == 0


@pytest.mark.parametrize("action", list(CalendarSyncAction))
async def test_a_missing_encryption_key_skips_rather_than_crashes(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    connect_calendar: ConnectCalendar,
    action: CalendarSyncAction,
) -> None:
    """Reachable only if the key was removed after connections were stored. Even then the
    worker keeps running rather than raising on every poll."""
    patient = await make_user()
    doctor = await make_doctor()
    await connect_calendar(patient)
    appointment = await make_appointment(db_session, patient, doctor)
    await make_job(db_session, appointment=appointment, user=patient, action=action, synced=True)

    report = await sync_once(db_session, InMemoryCalendarGateway(), None)

    assert report.skipped == 1
