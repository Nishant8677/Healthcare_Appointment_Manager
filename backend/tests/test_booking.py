"""The booking flow end to end: hold, confirm, cancel, reschedule and list."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Appointment, AppointmentStatus, DoctorProfile, User, UserRole
from app.models.doctor import DoctorLeaveDay

Headers = dict[str, str]
MakePatient = Callable[[], Awaitable[tuple[User, Headers]]]
MakeUser = Callable[..., Awaitable[User]]

SYMPTOMS = "Sore throat and mild fever for the last four days, worse in the evenings."


def a_future_day() -> datetime:
    return datetime.now(UTC) + timedelta(days=2)


def slot_at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day.date(), time(hour, minute), tzinfo=UTC)


def iso(value: datetime) -> str:
    """Render a timestamp for a request body."""
    return value.isoformat()


def moment(value: str) -> datetime:
    """Parse a timestamp from a response.

    Compared as instants, never as strings: the API emits `2026-09-01T09:00:00Z` while
    Python writes `+00:00` for the same moment, and a string comparison silently turns
    "these differ in format" into "these differ in value".
    """
    return datetime.fromisoformat(value)


async def hold(
    client: AsyncClient, headers: Headers, doctor: DoctorProfile, starts_at: datetime
) -> Any:
    return await client.post(
        "/appointments/hold",
        headers=headers,
        json={"doctor_id": str(doctor.id), "starts_at": iso(starts_at)},
    )


async def hold_and_confirm(
    client: AsyncClient, headers: Headers, doctor: DoctorProfile, starts_at: datetime
) -> dict[str, Any]:
    held = await hold(client, headers, doctor, starts_at)
    assert held.status_code == 201, held.text
    confirmed = await client.post(
        f"/appointments/{held.json()['id']}/confirm",
        headers=headers,
        json={"symptoms": SYMPTOMS, "duration_days": 4},
    )
    assert confirmed.status_code == 200, confirmed.text
    body: dict[str, Any] = confirmed.json()
    return body


# ---------------------------------------------------------------- availability endpoint


async def test_patient_can_see_free_slots(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    day = a_future_day()

    response = await client.get(
        f"/doctors/{bookable_doctor.id}/slots",
        headers=patient_headers,
        params={"date": day.date().isoformat()},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["slot_duration_minutes"] == 30
    assert len(body["slots"]) == 16


async def test_slots_for_an_unknown_doctor_are_not_found(
    client: AsyncClient, patient_headers: Headers
) -> None:
    response = await client.get(
        f"/doctors/{uuid.uuid4()}/slots",
        headers=patient_headers,
        params={"date": a_future_day().date().isoformat()},
    )

    assert response.status_code == 404


async def test_doctor_search_is_available_to_patients(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    response = await client.get(
        "/doctors", headers=patient_headers, params={"specialisation": "cardio"}
    )

    assert response.status_code == 200
    assert [doctor["id"] for doctor in response.json()] == [str(bookable_doctor.id)]


# ---------------------------------------------------------------- holding


async def test_holding_a_slot_reserves_it(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    starts_at = slot_at(a_future_day(), 9, 0)

    response = await hold(client, patient_headers, bookable_doctor, starts_at)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "held"
    assert body["hold_expires_at"] is not None
    assert body["symptom_report"] is None


async def test_a_held_slot_disappears_from_availability(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    day = a_future_day()
    starts_at = slot_at(day, 9, 0)
    await hold(client, patient_headers, bookable_doctor, starts_at)

    response = await client.get(
        f"/doctors/{bookable_doctor.id}/slots",
        headers=patient_headers,
        params={"date": day.date().isoformat()},
    )

    assert starts_at not in [moment(slot["starts_at"]) for slot in response.json()["slots"]]


async def test_a_time_that_is_not_a_slot_is_rejected(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    """Otherwise a patient could book 03:17 by posting a handcrafted time."""
    response = await hold(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 3, 17))

    assert response.status_code == 409


async def test_a_slot_in_the_past_is_rejected(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    yesterday = datetime.now(UTC) - timedelta(days=1)

    response = await hold(client, patient_headers, bookable_doctor, slot_at(yesterday, 9, 0))

    assert response.status_code == 409


async def test_a_slot_beyond_the_booking_horizon_is_rejected(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    far_future = datetime.now(UTC) + timedelta(days=400)

    response = await hold(client, patient_headers, bookable_doctor, slot_at(far_future, 9, 0))

    assert response.status_code == 409
    assert "days ahead" in response.json()["detail"]


async def test_a_leave_day_cannot_be_booked(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
) -> None:
    day = a_future_day()
    db_session.add(DoctorLeaveDay(doctor_profile_id=bookable_doctor.id, leave_date=day.date()))
    await db_session.commit()

    response = await hold(client, patient_headers, bookable_doctor, slot_at(day, 9, 0))

    assert response.status_code == 409


async def test_a_patient_may_only_hold_one_slot_at_a_time(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    """Otherwise one patient could reserve a doctor's entire day and release none of it."""
    day = a_future_day()
    first = await hold(client, patient_headers, bookable_doctor, slot_at(day, 9, 0))
    assert first.status_code == 201

    second = await hold(client, patient_headers, bookable_doctor, slot_at(day, 10, 0))

    assert second.status_code == 409
    assert "already have a slot on hold" in second.json()["detail"]


async def test_another_patient_cannot_hold_the_same_slot(
    client: AsyncClient, bookable_doctor: DoctorProfile, make_patient: MakePatient
) -> None:
    starts_at = slot_at(a_future_day(), 9, 0)
    _, first_headers = await make_patient()
    _, second_headers = await make_patient()

    assert (await hold(client, first_headers, bookable_doctor, starts_at)).status_code == 201
    second = await hold(client, second_headers, bookable_doctor, starts_at)

    assert second.status_code == 409


async def test_an_expired_hold_frees_the_slot_for_someone_else(
    client: AsyncClient,
    bookable_doctor: DoctorProfile,
    make_patient: MakePatient,
    db_session: AsyncSession,
) -> None:
    """The stale row is reclaimed at booking time — the unique index would otherwise block it."""
    starts_at = slot_at(a_future_day(), 9, 0)
    _, first_headers = await make_patient()
    _, second_headers = await make_patient()

    first = await hold(client, first_headers, bookable_doctor, starts_at)
    assert first.status_code == 201

    # Push the hold into the past, as if the patient wandered off.
    appointment = await db_session.get(Appointment, uuid.UUID(first.json()["id"]))
    assert appointment is not None
    appointment.hold_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()

    second = await hold(client, second_headers, bookable_doctor, starts_at)

    assert second.status_code == 201


async def test_a_doctor_cannot_book_an_appointment(
    client: AsyncClient,
    bookable_doctor: DoctorProfile,
    make_user: MakeUser,
    auth_header: Callable[[User], Headers],
) -> None:
    doctor_user = await make_user(role=UserRole.DOCTOR)

    response = await hold(
        client, auth_header(doctor_user), bookable_doctor, slot_at(a_future_day(), 9, 0)
    )

    assert response.status_code == 403


async def test_holding_requires_authentication(
    client: AsyncClient, bookable_doctor: DoctorProfile
) -> None:
    response = await client.post(
        "/appointments/hold",
        json={"doctor_id": str(bookable_doctor.id), "starts_at": iso(slot_at(a_future_day(), 9))},
    )

    assert response.status_code == 401


async def test_a_naive_timestamp_is_rejected(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    """A wall-clock time with no offset means different instants to different senders."""
    response = await client.post(
        "/appointments/hold",
        headers=patient_headers,
        json={
            "doctor_id": str(bookable_doctor.id),
            "starts_at": "2026-09-01T09:00:00",
        },
    )

    assert response.status_code == 422


# ---------------------------------------------------------------- confirming


async def test_confirming_records_the_symptom_form(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    body = await hold_and_confirm(
        client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0)
    )

    assert body["status"] == "confirmed"
    assert body["hold_expires_at"] is None
    assert body["symptom_report"]["symptoms"] == SYMPTOMS
    assert body["symptom_report"]["duration_days"] == 4


async def test_a_too_short_symptom_description_is_rejected(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    held = await hold(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))

    response = await client.post(
        f"/appointments/{held.json()['id']}/confirm",
        headers=patient_headers,
        json={"symptoms": "sore"},
    )

    assert response.status_code == 422


async def test_confirming_someone_elses_hold_reports_not_found(
    client: AsyncClient, bookable_doctor: DoctorProfile, make_patient: MakePatient
) -> None:
    """404 rather than 403: a distinct error would confirm the appointment id exists."""
    _, owner_headers = await make_patient()
    _, stranger_headers = await make_patient()
    held = await hold(client, owner_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))

    response = await client.post(
        f"/appointments/{held.json()['id']}/confirm",
        headers=stranger_headers,
        json={"symptoms": SYMPTOMS},
    )

    assert response.status_code == 404


async def test_confirming_twice_is_rejected(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    held = await hold(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    appointment_id = held.json()["id"]
    payload = {"symptoms": SYMPTOMS}

    first = await client.post(
        f"/appointments/{appointment_id}/confirm", headers=patient_headers, json=payload
    )
    second = await client.post(
        f"/appointments/{appointment_id}/confirm", headers=patient_headers, json=payload
    )

    assert first.status_code == 200
    assert second.status_code == 409


async def test_confirming_an_expired_hold_returns_gone(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
) -> None:
    """410 rather than 409: the client's correct next move is to start over, not retry."""
    held = await hold(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    appointment = await db_session.get(Appointment, uuid.UUID(held.json()["id"]))
    assert appointment is not None
    appointment.hold_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    response = await client.post(
        f"/appointments/{held.json()['id']}/confirm",
        headers=patient_headers,
        json={"symptoms": SYMPTOMS},
    )

    assert response.status_code == 410


# ---------------------------------------------------------------- cancelling


async def test_a_patient_can_cancel_their_own_appointment(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    booked = await hold_and_confirm(
        client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0)
    )

    response = await client.post(
        f"/appointments/{booked['id']}/cancel",
        headers=patient_headers,
        json={"reason": "Feeling better"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled_by_patient"


async def test_cancelling_frees_the_slot_again(
    client: AsyncClient, bookable_doctor: DoctorProfile, make_patient: MakePatient
) -> None:
    day = a_future_day()
    starts_at = slot_at(day, 9, 0)
    _, first_headers = await make_patient()
    _, second_headers = await make_patient()

    booked = await hold_and_confirm(client, first_headers, bookable_doctor, starts_at)
    await client.post(f"/appointments/{booked['id']}/cancel", headers=first_headers, json={})

    rebooked = await hold(client, second_headers, bookable_doctor, starts_at)

    assert rebooked.status_code == 201


async def test_a_stranger_cannot_cancel_an_appointment(
    client: AsyncClient, bookable_doctor: DoctorProfile, make_patient: MakePatient
) -> None:
    _, owner_headers = await make_patient()
    _, stranger_headers = await make_patient()
    booked = await hold_and_confirm(
        client, owner_headers, bookable_doctor, slot_at(a_future_day(), 9, 0)
    )

    response = await client.post(
        f"/appointments/{booked['id']}/cancel", headers=stranger_headers, json={}
    )

    assert response.status_code == 404


async def test_an_admin_cancelling_is_recorded_as_the_clinic(
    client: AsyncClient,
    patient_headers: Headers,
    admin_headers: Headers,
    bookable_doctor: DoctorProfile,
) -> None:
    """Phase 5 sends a different message depending on who cancelled."""
    booked = await hold_and_confirm(
        client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0)
    )

    response = await client.post(
        f"/appointments/{booked['id']}/cancel", headers=admin_headers, json={}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled_by_clinic"


async def test_cancelling_twice_is_rejected(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    booked = await hold_and_confirm(
        client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0)
    )

    await client.post(f"/appointments/{booked['id']}/cancel", headers=patient_headers, json={})
    second = await client.post(
        f"/appointments/{booked['id']}/cancel", headers=patient_headers, json={}
    )

    assert second.status_code == 409


# ---------------------------------------------------------------- rescheduling


async def test_rescheduling_moves_the_appointment_and_keeps_the_symptoms(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
) -> None:
    day = a_future_day()
    booked = await hold_and_confirm(client, patient_headers, bookable_doctor, slot_at(day, 9, 0))

    response = await client.post(
        f"/appointments/{booked['id']}/reschedule",
        headers=patient_headers,
        json={"starts_at": iso(slot_at(day, 14, 0))},
    )

    assert response.status_code == 200
    moved = response.json()
    assert moved["id"] != booked["id"]
    assert moved["status"] == "confirmed"
    assert moment(moved["starts_at"]) == slot_at(day, 14, 0)
    # The complaint has not changed just because the time did.
    assert moved["symptom_report"]["symptoms"] == SYMPTOMS

    original = await db_session.get(Appointment, uuid.UUID(booked["id"]))
    assert original is not None
    await db_session.refresh(original)
    assert original.status is AppointmentStatus.CANCELLED_BY_PATIENT


async def test_rescheduling_onto_a_taken_slot_is_rejected(
    client: AsyncClient, bookable_doctor: DoctorProfile, make_patient: MakePatient
) -> None:
    day = a_future_day()
    _, mine = await make_patient()
    _, theirs = await make_patient()

    booked = await hold_and_confirm(client, mine, bookable_doctor, slot_at(day, 9, 0))
    await hold_and_confirm(client, theirs, bookable_doctor, slot_at(day, 14, 0))

    response = await client.post(
        f"/appointments/{booked['id']}/reschedule",
        headers=mine,
        json={"starts_at": iso(slot_at(day, 14, 0))},
    )

    assert response.status_code == 409


async def test_a_held_appointment_cannot_be_rescheduled(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    day = a_future_day()
    held = await hold(client, patient_headers, bookable_doctor, slot_at(day, 9, 0))

    response = await client.post(
        f"/appointments/{held.json()['id']}/reschedule",
        headers=patient_headers,
        json={"starts_at": iso(slot_at(day, 14, 0))},
    )

    assert response.status_code == 409


# ---------------------------------------------------------------- listing


async def test_a_patient_sees_only_their_own_appointments(
    client: AsyncClient, bookable_doctor: DoctorProfile, make_patient: MakePatient
) -> None:
    day = a_future_day()
    _, mine = await make_patient()
    _, theirs = await make_patient()
    await hold_and_confirm(client, mine, bookable_doctor, slot_at(day, 9, 0))
    await hold_and_confirm(client, theirs, bookable_doctor, slot_at(day, 10, 0))

    response = await client.get("/appointments", headers=mine)

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert moment(response.json()[0]["starts_at"]) == slot_at(day, 9, 0)


async def test_a_doctor_sees_their_own_schedule(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    auth_header: Callable[[User], Headers],
) -> None:
    day = a_future_day()
    await hold_and_confirm(client, patient_headers, bookable_doctor, slot_at(day, 9, 0))
    doctor_user = await db_session.get(User, bookable_doctor.user_id)
    assert doctor_user is not None

    response = await client.get("/appointments", headers=auth_header(doctor_user))

    assert response.status_code == 200
    assert len(response.json()) == 1


async def test_cancelled_appointments_are_hidden_unless_asked_for(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    booked = await hold_and_confirm(
        client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0)
    )
    await client.post(f"/appointments/{booked['id']}/cancel", headers=patient_headers, json={})

    default_listing = await client.get("/appointments", headers=patient_headers)
    full_listing = await client.get(
        "/appointments", headers=patient_headers, params={"include_cancelled": True}
    )

    assert default_listing.json() == []
    assert len(full_listing.json()) == 1


async def test_confirmed_bookings_are_persisted_exactly_once(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
) -> None:
    starts_at = slot_at(a_future_day(), 9, 0)
    await hold_and_confirm(client, patient_headers, bookable_doctor, starts_at)

    result = await db_session.execute(
        select(Appointment).where(
            Appointment.doctor_profile_id == bookable_doctor.id,
            Appointment.starts_at == starts_at,
        )
    )

    assert len(result.scalars().all()) == 1
