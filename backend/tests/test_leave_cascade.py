"""Doctor leave over existing bookings.

The honest test of the outbox: one admin action has to cancel several appointments and
reliably tell each of those patients, atomically. These tests check both that it happens and
that it cannot happen by accident.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Appointment, AppointmentStatus, DoctorProfile, User
from app.models.enums import NotificationType
from app.models.notification import NotificationJob

Headers = dict[str, str]
MakePatient = Callable[[], Awaitable[tuple[User, Headers]]]

BASE = "/admin/doctors"
SYMPTOMS = "Sore throat and mild fever for the last four days, worse in the evenings."


def a_future_day() -> datetime:
    return datetime.now(UTC) + timedelta(days=3)


def slot_at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day.date(), time(hour, minute), tzinfo=UTC)


async def book(
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


async def hold_only(
    client: AsyncClient, headers: Headers, doctor: DoctorProfile, starts_at: datetime
) -> dict[str, Any]:
    held = await client.post(
        "/appointments/hold",
        headers=headers,
        json={"doctor_id": str(doctor.id), "starts_at": starts_at.isoformat()},
    )
    assert held.status_code == 201, held.text
    body: dict[str, Any] = held.json()
    return body


async def leave_payload(day: date, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"leave_date": day.isoformat()}
    payload.update(overrides)
    return payload


async def status_of(session: AsyncSession, appointment_id: str) -> AppointmentStatus:
    appointment = await session.get(Appointment, uuid.UUID(appointment_id))
    assert appointment is not None
    await session.refresh(appointment)
    return appointment.status


async def jobs_of_type(
    session: AsyncSession, notification_type: NotificationType
) -> list[NotificationJob]:
    result = await session.execute(
        select(NotificationJob).where(NotificationJob.notification_type == notification_type)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------- the preview


async def test_impact_lists_who_would_be_affected(
    client: AsyncClient,
    admin_headers: Headers,
    bookable_doctor: DoctorProfile,
    make_patient: MakePatient,
) -> None:
    """A bare count is not enough for an admin to decide responsibly."""
    day = a_future_day()
    _, first = await make_patient()
    _, second = await make_patient()
    await book(client, first, bookable_doctor, slot_at(day, 9, 0))
    await book(client, second, bookable_doctor, slot_at(day, 11, 30))

    response = await client.get(
        f"{BASE}/{bookable_doctor.id}/leave/impact",
        headers=admin_headers,
        params={"date": day.date().isoformat()},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["affected_count"] == 2
    assert len(body["appointments"]) == 2
    assert all(entry["patient_email"] for entry in body["appointments"])


async def test_the_preview_changes_nothing(
    client: AsyncClient,
    admin_headers: Headers,
    bookable_doctor: DoctorProfile,
    patient_headers: Headers,
    db_session: AsyncSession,
) -> None:
    day = a_future_day()
    booked = await book(client, patient_headers, bookable_doctor, slot_at(day, 9, 0))

    await client.get(
        f"{BASE}/{bookable_doctor.id}/leave/impact",
        headers=admin_headers,
        params={"date": day.date().isoformat()},
    )

    assert await status_of(db_session, booked["id"]) is AppointmentStatus.CONFIRMED


async def test_impact_on_a_clear_day_is_empty(
    client: AsyncClient, admin_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    response = await client.get(
        f"{BASE}/{bookable_doctor.id}/leave/impact",
        headers=admin_headers,
        params={"date": a_future_day().date().isoformat()},
    )

    assert response.json()["affected_count"] == 0


# ---------------------------------------------------------------- refusing by default


async def test_leave_is_refused_when_patients_are_booked(
    client: AsyncClient,
    admin_headers: Headers,
    bookable_doctor: DoctorProfile,
    patient_headers: Headers,
    db_session: AsyncSession,
) -> None:
    """Cancelling medical appointments must never be a side effect of recording a date."""
    day = a_future_day()
    booked = await book(client, patient_headers, bookable_doctor, slot_at(day, 9, 0))

    response = await client.post(
        f"{BASE}/{bookable_doctor.id}/leave",
        headers=admin_headers,
        json=await leave_payload(day.date()),
    )

    assert response.status_code == 409
    assert "1 appointment" in response.json()["detail"]
    # Nothing changed: no leave recorded, appointment untouched.
    assert await status_of(db_session, booked["id"]) is AppointmentStatus.CONFIRMED
    doctor = await client.get(f"{BASE}/{bookable_doctor.id}", headers=admin_headers)
    assert doctor.json()["leave_days"] == []


async def test_leave_on_a_clear_day_needs_no_acknowledgement(
    client: AsyncClient, admin_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    day = a_future_day()

    response = await client.post(
        f"{BASE}/{bookable_doctor.id}/leave",
        headers=admin_headers,
        json=await leave_payload(day.date()),
    )

    assert response.status_code == 201
    assert response.json()["cancelled_appointments"] == 0


# ---------------------------------------------------------------- the cascade


async def test_acknowledged_leave_cancels_and_notifies_everyone(
    client: AsyncClient,
    admin_headers: Headers,
    bookable_doctor: DoctorProfile,
    make_patient: MakePatient,
    db_session: AsyncSession,
) -> None:
    day = a_future_day()
    _, first = await make_patient()
    _, second = await make_patient()
    booked_one = await book(client, first, bookable_doctor, slot_at(day, 9, 0))
    booked_two = await book(client, second, bookable_doctor, slot_at(day, 14, 0))

    response = await client.post(
        f"{BASE}/{bookable_doctor.id}/leave",
        headers=admin_headers,
        json=await leave_payload(
            day.date(), reason="Conference", cancel_existing_appointments=True
        ),
    )

    assert response.status_code == 201
    assert response.json()["cancelled_appointments"] == 2
    assert response.json()["patients_notified"] == 2

    for appointment_id in (booked_one["id"], booked_two["id"]):
        assert await status_of(db_session, appointment_id) is AppointmentStatus.CANCELLED_BY_CLINIC

    notices = await jobs_of_type(db_session, NotificationType.LEAVE_CONFLICT)
    assert len(notices) == 2


async def test_the_cancellation_reason_names_the_doctor(
    client: AsyncClient,
    admin_headers: Headers,
    bookable_doctor: DoctorProfile,
    patient_headers: Headers,
    db_session: AsyncSession,
) -> None:
    day = a_future_day()
    booked = await book(client, patient_headers, bookable_doctor, slot_at(day, 9, 0))

    await client.post(
        f"{BASE}/{bookable_doctor.id}/leave",
        headers=admin_headers,
        json=await leave_payload(day.date(), cancel_existing_appointments=True),
    )

    appointment = await db_session.get(Appointment, uuid.UUID(booked["id"]))
    assert appointment is not None
    await db_session.refresh(appointment)
    assert "unavailable" in (appointment.cancellation_reason or "")


async def test_reminders_for_cancelled_appointments_are_dropped(
    client: AsyncClient,
    admin_headers: Headers,
    bookable_doctor: DoctorProfile,
    patient_headers: Headers,
    db_session: AsyncSession,
) -> None:
    """A reminder for an appointment the clinic cancelled would be worse than none."""
    day = a_future_day()
    await book(client, patient_headers, bookable_doctor, slot_at(day, 9, 0))
    assert len(await jobs_of_type(db_session, NotificationType.APPOINTMENT_REMINDER)) == 1

    await client.post(
        f"{BASE}/{bookable_doctor.id}/leave",
        headers=admin_headers,
        json=await leave_payload(day.date(), cancel_existing_appointments=True),
    )

    assert await jobs_of_type(db_session, NotificationType.APPOINTMENT_REMINDER) == []


async def test_a_held_slot_is_released_but_generates_no_email(
    client: AsyncClient,
    admin_headers: Headers,
    bookable_doctor: DoctorProfile,
    make_patient: MakePatient,
    db_session: AsyncSession,
) -> None:
    """A hold was never a booking; emailing about an appointment nobody made would confuse."""
    day = a_future_day()
    _, holder = await make_patient()
    held = await hold_only(client, holder, bookable_doctor, slot_at(day, 10, 0))

    response = await client.post(
        f"{BASE}/{bookable_doctor.id}/leave",
        headers=admin_headers,
        json=await leave_payload(day.date(), cancel_existing_appointments=True),
    )

    assert response.json()["cancelled_appointments"] == 1
    assert response.json()["patients_notified"] == 0
    assert await status_of(db_session, held["id"]) is AppointmentStatus.CANCELLED_BY_CLINIC
    assert await jobs_of_type(db_session, NotificationType.LEAVE_CONFLICT) == []


async def test_other_days_and_doctors_are_untouched(
    client: AsyncClient,
    admin_headers: Headers,
    bookable_doctor: DoctorProfile,
    make_patient: MakePatient,
    db_session: AsyncSession,
) -> None:
    """The cascade must be precise: leave on one day is not leave on every day."""
    leave_day = a_future_day()
    other_day = leave_day + timedelta(days=1)
    _, patient = await make_patient()
    doomed = await book(client, patient, bookable_doctor, slot_at(leave_day, 9, 0))
    survivor = await book(client, patient, bookable_doctor, slot_at(other_day, 9, 0))

    await client.post(
        f"{BASE}/{bookable_doctor.id}/leave",
        headers=admin_headers,
        json=await leave_payload(leave_day.date(), cancel_existing_appointments=True),
    )

    assert await status_of(db_session, doomed["id"]) is AppointmentStatus.CANCELLED_BY_CLINIC
    assert await status_of(db_session, survivor["id"]) is AppointmentStatus.CONFIRMED


async def test_the_day_offers_no_slots_afterwards(
    client: AsyncClient,
    admin_headers: Headers,
    bookable_doctor: DoctorProfile,
    patient_headers: Headers,
) -> None:
    day = a_future_day()
    await book(client, patient_headers, bookable_doctor, slot_at(day, 9, 0))
    await client.post(
        f"{BASE}/{bookable_doctor.id}/leave",
        headers=admin_headers,
        json=await leave_payload(day.date(), cancel_existing_appointments=True),
    )

    slots = await client.get(
        f"/doctors/{bookable_doctor.id}/slots",
        headers=patient_headers,
        params={"date": day.date().isoformat()},
    )

    assert slots.json()["slots"] == []


async def test_a_freed_slot_cannot_be_rebooked_that_day(
    client: AsyncClient,
    admin_headers: Headers,
    bookable_doctor: DoctorProfile,
    make_patient: MakePatient,
) -> None:
    """Cancelling normally frees the slot; cancelling *because of leave* must not."""
    day = a_future_day()
    _, first = await make_patient()
    _, second = await make_patient()
    await book(client, first, bookable_doctor, slot_at(day, 9, 0))
    await client.post(
        f"{BASE}/{bookable_doctor.id}/leave",
        headers=admin_headers,
        json=await leave_payload(day.date(), cancel_existing_appointments=True),
    )

    attempt = await client.post(
        "/appointments/hold",
        headers=second,
        json={
            "doctor_id": str(bookable_doctor.id),
            "starts_at": slot_at(day, 9, 0).isoformat(),
        },
    )

    assert attempt.status_code == 409


async def test_the_notice_tells_the_patient_to_rebook(
    client: AsyncClient,
    admin_headers: Headers,
    bookable_doctor: DoctorProfile,
    patient_headers: Headers,
    db_session: AsyncSession,
) -> None:
    from app.services import notifications

    day = a_future_day()
    await book(client, patient_headers, bookable_doctor, slot_at(day, 9, 0))
    await client.post(
        f"{BASE}/{bookable_doctor.id}/leave",
        headers=admin_headers,
        json=await leave_payload(day.date(), cancel_existing_appointments=True),
    )

    notice = (await jobs_of_type(db_session, NotificationType.LEAVE_CONFLICT))[0]
    message = notifications.render(notice)

    assert "unavailable" in message.body
    assert "book another time" in message.body
    # Names a concrete next step rather than leaving the patient to work it out.
    assert bookable_doctor.specialisation in message.body
    assert notice.payload["doctor_profile_id"] == str(bookable_doctor.id)


# ---------------------------------------------------------------- unchanged rules


async def test_past_leave_is_still_rejected(
    client: AsyncClient, admin_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    response = await client.post(
        f"{BASE}/{bookable_doctor.id}/leave",
        headers=admin_headers,
        json=await leave_payload(date.today() - timedelta(days=1)),
    )

    assert response.status_code == 422


async def test_duplicate_leave_is_still_rejected(
    client: AsyncClient, admin_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    day = a_future_day().date()
    payload = await leave_payload(day)

    first = await client.post(
        f"{BASE}/{bookable_doctor.id}/leave", headers=admin_headers, json=payload
    )
    second = await client.post(
        f"{BASE}/{bookable_doctor.id}/leave", headers=admin_headers, json=payload
    )

    assert first.status_code == 201
    assert second.status_code == 409


async def test_removing_leave_does_not_resurrect_appointments(
    client: AsyncClient,
    admin_headers: Headers,
    bookable_doctor: DoctorProfile,
    patient_headers: Headers,
    db_session: AsyncSession,
) -> None:
    """Those patients were told it was cancelled. Silently reinstating it would be worse."""
    day = a_future_day()
    booked = await book(client, patient_headers, bookable_doctor, slot_at(day, 9, 0))
    recorded = await client.post(
        f"{BASE}/{bookable_doctor.id}/leave",
        headers=admin_headers,
        json=await leave_payload(day.date(), cancel_existing_appointments=True),
    )

    removed = await client.delete(
        f"{BASE}/{bookable_doctor.id}/leave/{recorded.json()['id']}", headers=admin_headers
    )

    assert removed.status_code == 204
    assert await status_of(db_session, booked["id"]) is AppointmentStatus.CANCELLED_BY_CLINIC
    # The slot is bookable again, though.
    slots = await client.get(
        f"/doctors/{bookable_doctor.id}/slots",
        headers=patient_headers,
        params={"date": day.date().isoformat()},
    )
    assert len(slots.json()["slots"]) > 0


async def test_patients_cannot_record_leave(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    response = await client.post(
        f"{BASE}/{bookable_doctor.id}/leave",
        headers=patient_headers,
        json=await leave_payload(a_future_day().date()),
    )

    assert response.status_code == 403
