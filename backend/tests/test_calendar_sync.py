"""What the calendar reconciler records when an appointment changes.

These tests are about *desired state*, which is the whole design. The property being
protected is that after any sequence of changes there is exactly one row per calendar per
appointment, saying what that calendar should currently show — never a backlog of operations
whose order decides the outcome.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DoctorProfile, User
from app.models.calendar import CalendarConnection, CalendarSyncJob
from app.models.enums import CalendarSyncAction, CalendarSyncStatus
from app.services.google_calendar import derive_event_id

Headers = dict[str, str]
MakePatient = Callable[[], Awaitable[tuple[User, Headers]]]
ConnectCalendar = Callable[..., Awaitable[CalendarConnection]]

SYMPTOMS = "Sharp chest pain when climbing stairs, three days, worse at night."


def a_future_day() -> datetime:
    return datetime.now(UTC) + timedelta(days=3)


def slot_at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day.date(), time(hour, minute), tzinfo=UTC)


async def hold_and_confirm(
    client: AsyncClient, headers: Headers, doctor: DoctorProfile, starts_at: datetime
) -> dict[str, Any]:
    held = await client.post(
        "/appointments/hold",
        headers=headers,
        json={"doctor_id": str(doctor.id), "starts_at": starts_at.isoformat()},
    )
    assert held.status_code == 201, held.text
    confirmed = await client.post(
        f"/appointments/{held.json()['id']}/confirm",
        headers=headers,
        json={"symptoms": SYMPTOMS},
    )
    assert confirmed.status_code == 200, confirmed.text
    body: dict[str, Any] = confirmed.json()
    return body


async def sync_jobs(session: AsyncSession) -> list[CalendarSyncJob]:
    result = await session.execute(select(CalendarSyncJob).order_by(CalendarSyncJob.created_at))
    return list(result.scalars().all())


async def doctor_user(session: AsyncSession, doctor: DoctorProfile) -> User:
    user = await session.get(User, doctor.user_id)
    assert user is not None
    return user


# --------------------------------------------------------------- nothing without a connection


async def test_booking_writes_no_calendar_rows_when_nobody_has_connected(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
) -> None:
    """The default state of the system. A clinic that never configures Google must not
    accumulate a permanent trail of rows that could only ever be skipped."""
    await hold_and_confirm(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9))

    assert await sync_jobs(db_session) == []


async def test_booking_queues_only_for_the_participant_who_connected(
    client: AsyncClient,
    make_patient: MakePatient,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    connect_calendar: ConnectCalendar,
) -> None:
    patient, headers = await make_patient()
    await connect_calendar(patient)

    await hold_and_confirm(client, headers, bookable_doctor, slot_at(a_future_day(), 10))

    jobs = await sync_jobs(db_session)
    assert len(jobs) == 1
    assert jobs[0].user_id == patient.id
    assert jobs[0].action is CalendarSyncAction.SYNC
    assert jobs[0].status is CalendarSyncStatus.PENDING


async def test_both_participants_get_their_own_row_and_their_own_event(
    client: AsyncClient,
    make_patient: MakePatient,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    connect_calendar: ConnectCalendar,
) -> None:
    """Separate events on separate calendars, not one event with the other as an attendee —
    an invitation would produce a second entry and a competing email."""
    patient, headers = await make_patient()
    await connect_calendar(patient)
    await connect_calendar(await doctor_user(db_session, bookable_doctor))

    body = await hold_and_confirm(client, headers, bookable_doctor, slot_at(a_future_day(), 11))

    jobs = await sync_jobs(db_session)
    assert len(jobs) == 2
    assert {job.user_id for job in jobs} == {patient.id, bookable_doctor.user_id}
    assert len({job.google_event_id for job in jobs}) == 2
    for job in jobs:
        assert job.google_event_id == derive_event_id(uuid.UUID(body["id"]), job.user_id)


async def test_a_revoked_connection_queues_nothing(
    client: AsyncClient,
    make_patient: MakePatient,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    connect_calendar: ConnectCalendar,
) -> None:
    patient, headers = await make_patient()
    await connect_calendar(patient, revoked=True)

    await hold_and_confirm(client, headers, bookable_doctor, slot_at(a_future_day(), 12))

    assert await sync_jobs(db_session) == []


# --------------------------------------------------------------- what the event says


async def test_the_event_never_carries_the_symptom_text(
    client: AsyncClient,
    make_patient: MakePatient,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    connect_calendar: ConnectCalendar,
) -> None:
    """A calendar entry syncs to phones and lock screens. Somebody's medical complaint does
    not belong on one, however convenient it would be for the doctor."""
    patient, headers = await make_patient()
    await connect_calendar(patient)

    await hold_and_confirm(client, headers, bookable_doctor, slot_at(a_future_day(), 13))

    payload = (await sync_jobs(db_session))[0].payload
    serialised = f"{payload['summary']} {payload['description']}"
    assert SYMPTOMS not in serialised
    assert "chest pain" not in serialised.lower()


async def test_each_side_sees_the_other_partys_name(
    client: AsyncClient,
    make_patient: MakePatient,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    connect_calendar: ConnectCalendar,
) -> None:
    """The doctor's own copy must not read "Appointment with Dr Test" — naming them to
    themselves — which is the same mistake the confirmation emails once made."""
    patient, headers = await make_patient()
    await connect_calendar(patient)
    await connect_calendar(await doctor_user(db_session, bookable_doctor))

    await hold_and_confirm(client, headers, bookable_doctor, slot_at(a_future_day(), 14))

    jobs = {job.user_id: job for job in await sync_jobs(db_session)}
    assert "Dr Test" in jobs[patient.id].payload["summary"]
    assert patient.full_name in jobs[bookable_doctor.user_id].payload["summary"]
    assert "Dr Test" not in jobs[bookable_doctor.user_id].payload["summary"]


# --------------------------------------------------------------- cancelling


async def test_cancelling_rewrites_the_row_rather_than_adding_one(
    client: AsyncClient,
    make_patient: MakePatient,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    connect_calendar: ConnectCalendar,
) -> None:
    """The core of the design. A command queue would now hold a create *and* a delete, and
    the outcome would depend on which a worker picked up first."""
    patient, headers = await make_patient()
    await connect_calendar(patient)
    body = await hold_and_confirm(client, headers, bookable_doctor, slot_at(a_future_day(), 15))

    cancelled = await client.post(f"/appointments/{body['id']}/cancel", headers=headers, json={})
    assert cancelled.status_code == 200, cancelled.text

    jobs = await sync_jobs(db_session)
    assert len(jobs) == 1
    assert jobs[0].action is CalendarSyncAction.DELETE
    assert jobs[0].status is CalendarSyncStatus.PENDING


async def test_a_cancellation_resets_the_retry_budget(
    client: AsyncClient,
    make_patient: MakePatient,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    connect_calendar: ConnectCalendar,
) -> None:
    """Failures recorded against "create this event" say nothing about "delete it"."""
    patient, headers = await make_patient()
    await connect_calendar(patient)
    body = await hold_and_confirm(client, headers, bookable_doctor, slot_at(a_future_day(), 16))

    job = (await sync_jobs(db_session))[0]
    job.attempts = 3
    job.status = CalendarSyncStatus.FAILED
    job.last_error = "Google was down"
    await db_session.commit()

    await client.post(f"/appointments/{body['id']}/cancel", headers=headers, json={})

    await db_session.refresh(job)
    assert job.attempts == 0
    assert job.status is CalendarSyncStatus.PENDING
    assert job.last_error is None


async def test_doctor_leave_marks_the_calendar_entries_for_removal(
    client: AsyncClient,
    make_patient: MakePatient,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    connect_calendar: ConnectCalendar,
    admin_headers: Headers,
) -> None:
    """A cancelled clinic day has to clear both parties' calendars, or they keep an entry
    for a consultation the clinic has already called off."""
    patient, headers = await make_patient()
    await connect_calendar(patient)
    await connect_calendar(await doctor_user(db_session, bookable_doctor))
    day = a_future_day()
    await hold_and_confirm(client, headers, bookable_doctor, slot_at(day, 9, 30))

    response = await client.post(
        f"/admin/doctors/{bookable_doctor.id}/leave",
        headers=admin_headers,
        json={"leave_date": day.date().isoformat(), "cancel_existing_appointments": True},
    )
    assert response.status_code == 201, response.text

    jobs = await sync_jobs(db_session)
    assert len(jobs) == 2
    assert all(job.action is CalendarSyncAction.DELETE for job in jobs)


# --------------------------------------------------------------- rescheduling


async def test_rescheduling_moves_the_existing_event_instead_of_replacing_it(
    client: AsyncClient,
    make_patient: MakePatient,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    connect_calendar: ConnectCalendar,
) -> None:
    """A reschedule creates a new appointment row, so the naive implementation deletes one
    event and creates another — which on a phone reads as a cancellation followed by a
    surprise booking. Keeping the event id makes the entry move instead."""
    patient, headers = await make_patient()
    await connect_calendar(patient)
    day = a_future_day()
    body = await hold_and_confirm(client, headers, bookable_doctor, slot_at(day, 10, 30))

    original_event_id = (await sync_jobs(db_session))[0].google_event_id

    moved = await client.post(
        f"/appointments/{body['id']}/reschedule",
        headers=headers,
        json={"starts_at": slot_at(day, 14, 30).isoformat()},
    )
    assert moved.status_code == 200, moved.text

    jobs = await sync_jobs(db_session)
    assert len(jobs) == 1, "the row moved to the replacement rather than being duplicated"
    assert jobs[0].google_event_id == original_event_id
    assert jobs[0].appointment_id == uuid.UUID(moved.json()["id"])
    assert jobs[0].action is CalendarSyncAction.SYNC
    assert jobs[0].payload["starts_at"].startswith(slot_at(day, 14, 30).isoformat()[:16])


async def test_rescheduling_leaves_no_row_pointing_at_the_cancelled_appointment(
    client: AsyncClient,
    make_patient: MakePatient,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    connect_calendar: ConnectCalendar,
) -> None:
    patient, headers = await make_patient()
    await connect_calendar(patient)
    day = a_future_day()
    body = await hold_and_confirm(client, headers, bookable_doctor, slot_at(day, 11, 30))
    original_id = uuid.UUID(body["id"])

    await client.post(
        f"/appointments/{body['id']}/reschedule",
        headers=headers,
        json={"starts_at": slot_at(day, 15, 30).isoformat()},
    )

    jobs = await sync_jobs(db_session)
    assert all(job.appointment_id != original_id for job in jobs)


async def test_rescheduling_queues_a_participant_who_connected_in_the_meantime(
    client: AsyncClient,
    make_patient: MakePatient,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    connect_calendar: ConnectCalendar,
) -> None:
    """There is no event to move for someone who had no connection when it was booked, so
    this is an ordinary create rather than a gap."""
    patient, headers = await make_patient()
    await connect_calendar(patient)
    day = a_future_day()
    body = await hold_and_confirm(client, headers, bookable_doctor, slot_at(day, 12, 30))

    await connect_calendar(await doctor_user(db_session, bookable_doctor))

    await client.post(
        f"/appointments/{body['id']}/reschedule",
        headers=headers,
        json={"starts_at": slot_at(day, 16, 30).isoformat()},
    )

    jobs = await sync_jobs(db_session)
    assert {job.user_id for job in jobs} == {patient.id, bookable_doctor.user_id}
    assert all(job.action is CalendarSyncAction.SYNC for job in jobs)


# --------------------------------------------------------------- the invariant


async def test_one_row_per_calendar_survives_a_long_sequence_of_changes(
    client: AsyncClient,
    make_patient: MakePatient,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    connect_calendar: ConnectCalendar,
) -> None:
    """Book, reschedule twice, then cancel. Whatever happened, one calendar means one row."""
    patient, headers = await make_patient()
    await connect_calendar(patient)
    day = a_future_day()
    body = await hold_and_confirm(client, headers, bookable_doctor, slot_at(day, 9))

    current = body["id"]
    for hour in (10, 11):
        moved = await client.post(
            f"/appointments/{current}/reschedule",
            headers=headers,
            json={"starts_at": slot_at(day, hour).isoformat()},
        )
        assert moved.status_code == 200, moved.text
        current = moved.json()["id"]

    await client.post(f"/appointments/{current}/cancel", headers=headers, json={})

    jobs = await sync_jobs(db_session)
    assert len(jobs) == 1
    assert jobs[0].user_id == patient.id
    assert jobs[0].action is CalendarSyncAction.DELETE
    assert jobs[0].appointment_id == uuid.UUID(current)
