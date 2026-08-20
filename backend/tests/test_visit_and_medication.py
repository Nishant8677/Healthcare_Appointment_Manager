"""Recording a visit, and the medication reminders that follow from it.

The rule this file guards: reminders come from the prescription's structured fields, never
from generated text. A dosing schedule parsed out of an LLM's prose is a medication error
waiting to happen.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Appointment, AppointmentStatus, DoctorProfile, User
from app.models.enums import NotificationType, SummaryStatus, SummaryType
from app.models.notification import NotificationJob
from app.services.notifications import dose_times

Headers = dict[str, str]

SYMPTOMS = "Sore throat and mild fever for the last four days, worse in the evenings."
NOTES = "Viral pharyngitis. Rest, fluids, paracetamol as needed. Review if fever persists."


def a_future_day() -> datetime:
    return datetime.now(UTC) + timedelta(days=2)


def slot_at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day.date(), time(hour, minute), tzinfo=UTC)


def visit_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "clinical_notes": NOTES,
        "medications": [
            {
                "drug_name": "Paracetamol",
                "dosage": "500mg",
                "times_per_day": 3,
                "duration_days": 5,
                "instructions": "After food",
            }
        ],
    }
    payload.update(overrides)
    return payload


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


async def doctor_headers_for(
    session: AsyncSession, doctor: DoctorProfile, auth_header: Callable[[User], Headers]
) -> Headers:
    user = await session.get(User, doctor.user_id)
    assert user is not None
    return auth_header(user)


async def reminders_for(session: AsyncSession, appointment_id: str) -> list[NotificationJob]:
    result = await session.execute(
        select(NotificationJob)
        .where(
            NotificationJob.appointment_id == uuid.UUID(appointment_id),
            NotificationJob.notification_type == NotificationType.MEDICATION_REMINDER,
        )
        .order_by(NotificationJob.scheduled_for)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------- recording the visit


async def test_a_doctor_can_record_the_visit(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    auth_header: Callable[[User], Headers],
) -> None:
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    headers = await doctor_headers_for(db_session, bookable_doctor, auth_header)

    response = await client.post(
        f"/appointments/{booked['id']}/visit", headers=headers, json=visit_payload()
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "completed"
    assert response.json()["reminders_scheduled"] == 15  # 3 a day for 5 days


async def test_recording_a_visit_queues_a_pending_patient_summary(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    auth_header: Callable[[User], Headers],
) -> None:
    """The doctor is not made to wait on a language model before seeing the next patient."""
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    headers = await doctor_headers_for(db_session, bookable_doctor, auth_header)

    await client.post(f"/appointments/{booked['id']}/visit", headers=headers, json=visit_payload())

    summary = await client.get(
        f"/appointments/{booked['id']}/post-visit-summary", headers=patient_headers
    )
    assert summary.status_code == 200
    assert summary.json()["status"] == SummaryStatus.PENDING.value
    assert summary.json()["summary_type"] == SummaryType.POST_VISIT.value


async def test_a_patient_cannot_record_clinical_notes(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))

    response = await client.post(
        f"/appointments/{booked['id']}/visit", headers=patient_headers, json=visit_payload()
    )

    assert response.status_code == 403


async def test_another_doctor_cannot_record_the_visit(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    make_doctor: Callable[..., Awaitable[DoctorProfile]],
    db_session: AsyncSession,
    auth_header: Callable[[User], Headers],
) -> None:
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    other = await make_doctor(specialisation="Dermatology")
    headers = await doctor_headers_for(db_session, other, auth_header)

    response = await client.post(
        f"/appointments/{booked['id']}/visit", headers=headers, json=visit_payload()
    )

    assert response.status_code == 404


async def test_the_visit_cannot_be_recorded_twice(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    auth_header: Callable[[User], Headers],
) -> None:
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    headers = await doctor_headers_for(db_session, bookable_doctor, auth_header)

    first = await client.post(
        f"/appointments/{booked['id']}/visit", headers=headers, json=visit_payload()
    )
    second = await client.post(
        f"/appointments/{booked['id']}/visit", headers=headers, json=visit_payload()
    )

    assert first.status_code == 201
    assert second.status_code == 409


async def test_a_cancelled_appointment_cannot_be_completed(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    auth_header: Callable[[User], Headers],
) -> None:
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    await client.post(f"/appointments/{booked['id']}/cancel", headers=patient_headers, json={})
    headers = await doctor_headers_for(db_session, bookable_doctor, auth_header)

    response = await client.post(
        f"/appointments/{booked['id']}/visit", headers=headers, json=visit_payload()
    )

    assert response.status_code == 409


async def test_notes_that_are_too_short_are_rejected(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    auth_header: Callable[[User], Headers],
) -> None:
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    headers = await doctor_headers_for(db_session, bookable_doctor, auth_header)

    response = await client.post(
        f"/appointments/{booked['id']}/visit",
        headers=headers,
        json=visit_payload(clinical_notes="ok"),
    )

    assert response.status_code == 422


# ---------------------------------------------------------------- medication reminders


async def test_reminders_come_from_the_structured_fields(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    auth_header: Callable[[User], Headers],
) -> None:
    """Three a day for five days is fifteen reminders — arithmetic, not interpretation."""
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    headers = await doctor_headers_for(db_session, bookable_doctor, auth_header)

    await client.post(f"/appointments/{booked['id']}/visit", headers=headers, json=visit_payload())

    queued = await reminders_for(db_session, booked["id"])
    assert len(queued) == 15
    assert all(job.payload["drug_name"] == "Paracetamol" for job in queued)
    assert all(job.payload["dosage"] == "500mg" for job in queued)
    assert all(job.scheduled_for > datetime.now(UTC) for job in queued)


async def test_no_medication_means_no_reminders(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    auth_header: Callable[[User], Headers],
) -> None:
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    headers = await doctor_headers_for(db_session, bookable_doctor, auth_header)

    response = await client.post(
        f"/appointments/{booked['id']}/visit", headers=headers, json=visit_payload(medications=[])
    )

    assert response.json()["reminders_scheduled"] == 0
    assert await reminders_for(db_session, booked["id"]) == []


async def test_a_long_course_is_capped_rather_than_queueing_thousands(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    auth_header: Callable[[User], Headers],
    settings: Any,
) -> None:
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    headers = await doctor_headers_for(db_session, bookable_doctor, auth_header)

    response = await client.post(
        f"/appointments/{booked['id']}/visit",
        headers=headers,
        json=visit_payload(
            medications=[
                {
                    "drug_name": "Metformin",
                    "dosage": "500mg",
                    "times_per_day": 2,
                    "duration_days": 365,
                }
            ]
        ),
    )

    capped = settings.medication_reminder_max_days * 2
    assert response.json()["reminders_scheduled"] == capped


async def test_the_appointment_reminder_is_dropped_once_the_visit_happened(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    auth_header: Callable[[User], Headers],
) -> None:
    """Reminding someone about an appointment they already attended is noise."""
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    headers = await doctor_headers_for(db_session, bookable_doctor, auth_header)

    await client.post(f"/appointments/{booked['id']}/visit", headers=headers, json=visit_payload())

    result = await db_session.execute(
        select(NotificationJob).where(
            NotificationJob.appointment_id == uuid.UUID(booked["id"]),
            NotificationJob.notification_type == NotificationType.APPOINTMENT_REMINDER,
        )
    )
    assert result.scalars().all() == []


async def test_a_course_prescribed_late_in_the_day_still_gets_every_dose(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    auth_header: Callable[[User], Headers],
) -> None:
    """Regression: doses already past today were skipped, so a course prescribed at 4pm
    quietly delivered fewer reminders than the patient owed — and how many depended on what
    time of day it happened to be."""
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    headers = await doctor_headers_for(db_session, bookable_doctor, auth_header)

    response = await client.post(
        f"/appointments/{booked['id']}/visit", headers=headers, json=visit_payload()
    )

    # Three a day for five days is fifteen doses, whatever the clock says right now.
    assert response.json()["reminders_scheduled"] == 15
    queued = await reminders_for(db_session, booked["id"])
    assert len(queued) == 15
    assert len({job.scheduled_for for job in queued}) == 15, "reminders must not collide"


@pytest.mark.parametrize(
    ("doses", "expected"),
    [
        (1, [14]),
        (2, [8, 20]),
        (3, [8, 14, 20]),
        (4, [8, 12, 16, 20]),
        (5, [8, 11, 14, 17, 20]),
    ],
)
def test_doses_are_spread_across_the_waking_day(doses: int, expected: list[int]) -> None:
    """Predictable times a patient can build a habit around, not arbitrary ones."""
    assert dose_times(doses, first_hour=8, last_hour=20) == expected


async def test_completing_a_visit_marks_the_appointment(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    auth_header: Callable[[User], Headers],
) -> None:
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    headers = await doctor_headers_for(db_session, bookable_doctor, auth_header)

    await client.post(f"/appointments/{booked['id']}/visit", headers=headers, json=visit_payload())

    appointment = await db_session.get(Appointment, uuid.UUID(booked["id"]))
    assert appointment is not None
    await db_session.refresh(appointment)
    assert appointment.status is AppointmentStatus.COMPLETED
