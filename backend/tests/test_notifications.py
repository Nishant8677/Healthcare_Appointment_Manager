"""The notification outbox: what gets queued, and what the templates say."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DoctorProfile, User
from app.models.enums import NotificationStatus, NotificationType
from app.models.notification import NotificationJob
from app.services import notifications

Headers = dict[str, str]
MakePatient = Callable[[], Awaitable[tuple[User, Headers]]]

SYMPTOMS = "Sore throat and mild fever for the last four days, worse in the evenings."


def a_future_day() -> datetime:
    return datetime.now(UTC) + timedelta(days=2)


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


async def jobs_for(session: AsyncSession, appointment_id: str) -> list[NotificationJob]:
    result = await session.execute(
        select(NotificationJob)
        .where(NotificationJob.appointment_id == uuid.UUID(appointment_id))
        .order_by(NotificationJob.notification_type, NotificationJob.scheduled_for)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------- what gets queued


async def test_confirming_queues_messages_for_both_sides(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
) -> None:
    booked = await hold_and_confirm(
        client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0)
    )

    queued = await jobs_for(db_session, booked["id"])
    confirmations = [
        job for job in queued if job.notification_type is NotificationType.BOOKING_CONFIRMATION
    ]

    assert len(confirmations) == 2
    assert all(job.status is NotificationStatus.PENDING for job in confirmations)
    assert all(job.attempts == 0 for job in confirmations)


async def test_confirming_also_queues_the_reminder_dormant(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
) -> None:
    """The reminder is queued immediately but must not go out until it is due."""
    starts_at = slot_at(a_future_day(), 9, 0)
    booked = await hold_and_confirm(client, patient_headers, bookable_doctor, starts_at)

    reminders = [
        job
        for job in await jobs_for(db_session, booked["id"])
        if job.notification_type is NotificationType.APPOINTMENT_REMINDER
    ]

    assert len(reminders) == 1
    assert reminders[0].scheduled_for == starts_at - timedelta(hours=24)
    assert reminders[0].scheduled_for > datetime.now(UTC)


async def test_holding_alone_queues_nothing(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
) -> None:
    """A reservation is not a booking; nobody should be emailed about one."""
    held = await client.post(
        "/appointments/hold",
        headers=patient_headers,
        json={
            "doctor_id": str(bookable_doctor.id),
            "starts_at": slot_at(a_future_day(), 9, 0).isoformat(),
        },
    )

    assert await jobs_for(db_session, held.json()["id"]) == []


async def test_cancelling_queues_messages_and_drops_the_reminder(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
) -> None:
    """A reminder for an appointment that is not happening must never be sent."""
    booked = await hold_and_confirm(
        client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0)
    )
    await client.post(
        f"/appointments/{booked['id']}/cancel", headers=patient_headers, json={"reason": "Better"}
    )

    queued = await jobs_for(db_session, booked["id"])
    by_type = {job.notification_type for job in queued}

    assert NotificationType.APPOINTMENT_REMINDER not in by_type
    assert len([j for j in queued if j.notification_type is NotificationType.CANCELLATION]) == 2


async def test_a_clinic_cancellation_is_worded_differently(
    client: AsyncClient,
    patient_headers: Headers,
    admin_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
) -> None:
    booked = await hold_and_confirm(
        client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0)
    )
    await client.post(f"/appointments/{booked['id']}/cancel", headers=admin_headers, json={})

    cancellation = next(
        job
        for job in await jobs_for(db_session, booked["id"])
        if job.notification_type is NotificationType.CANCELLATION
    )
    message = notifications.render(cancellation)

    assert cancellation.payload["cancelled_by_clinic"] is True
    assert "clinic has had to cancel" in message.body


async def test_rescheduling_queues_a_cancellation_and_a_new_confirmation(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
) -> None:
    day = a_future_day()
    booked = await hold_and_confirm(client, patient_headers, bookable_doctor, slot_at(day, 9, 0))

    moved = await client.post(
        f"/appointments/{booked['id']}/reschedule",
        headers=patient_headers,
        json={"starts_at": slot_at(day, 14, 0).isoformat()},
    )
    assert moved.status_code == 200

    old_types = {job.notification_type for job in await jobs_for(db_session, booked["id"])}
    new_types = {job.notification_type for job in await jobs_for(db_session, moved.json()["id"])}

    # The original keeps the confirmations it legitimately raised when first booked — those
    # may already have been delivered — and gains a cancellation.
    assert NotificationType.CANCELLATION in old_types
    # Its reminder must be gone: that appointment is no longer happening.
    assert NotificationType.APPOINTMENT_REMINDER not in old_types
    # The replacement gets its own confirmation and its own reminder.
    assert NotificationType.BOOKING_CONFIRMATION in new_types
    assert NotificationType.APPOINTMENT_REMINDER in new_types


async def test_a_queued_message_carries_everything_it_needs(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
) -> None:
    """Denormalised on purpose: rendering must not depend on rows that may have changed."""
    booked = await hold_and_confirm(
        client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0)
    )

    job = (await jobs_for(db_session, booked["id"]))[0]

    for key in ("doctor_name", "specialisation", "starts_at", "starts_at_local", "recipient_name"):
        assert key in job.payload, f"payload is missing {key}"


# ---------------------------------------------------------------- rendering


def _job(notification_type: NotificationType, **payload: Any) -> NotificationJob:
    base = {
        "recipient_name": "Meera Nair",
        "doctor_name": "Dr Asha Rao",
        "specialisation": "Cardiology",
        "starts_at_local": "Thursday 03 September 2026 at 16:30",
        "timezone": "Asia/Kolkata",
    }
    base.update(payload)
    return NotificationJob(
        notification_type=notification_type,
        status=NotificationStatus.PENDING,
        recipient_email="meera@example.com",
        payload=base,
        scheduled_for=datetime.now(UTC),
    )


def test_confirmation_names_the_time_and_doctor() -> None:
    message = notifications.render(_job(NotificationType.BOOKING_CONFIRMATION))

    assert "Meera Nair" in message.body
    assert "Dr Asha Rao" in message.body
    assert "Thursday 03 September 2026 at 16:30" in message.body
    assert message.to_address == "meera@example.com"


def test_the_doctors_copy_names_the_patient_not_the_doctor() -> None:
    """A doctor reading "your appointment with Dr Asha Rao" when they are Dr Asha Rao is
    exactly the detail that makes a working system look unfinished."""
    doctor_copy = notifications.render(
        _job(
            NotificationType.BOOKING_CONFIRMATION,
            recipient_name="Dr Asha Rao",
            recipient_is_doctor=True,
            patient_name="Meera Nair",
        )
    )

    assert "with Meera Nair" in doctor_copy.body
    assert "with Dr Asha Rao" not in doctor_copy.body


def test_the_patients_copy_names_the_doctor() -> None:
    patient_copy = notifications.render(
        _job(
            NotificationType.BOOKING_CONFIRMATION,
            recipient_name="Meera Nair",
            recipient_is_doctor=False,
            patient_name="Meera Nair",
        )
    )

    assert "with Dr Asha Rao" in patient_copy.body
    assert "Cardiology" in patient_copy.body


async def test_each_side_of_a_confirmation_is_addressed_correctly(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
) -> None:
    booked = await hold_and_confirm(
        client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0)
    )

    confirmations = [
        job
        for job in await jobs_for(db_session, booked["id"])
        if job.notification_type is NotificationType.BOOKING_CONFIRMATION
    ]
    flags = sorted(bool(job.payload["recipient_is_doctor"]) for job in confirmations)

    assert flags == [False, True], "one copy for the patient, one for the doctor"


def test_reminder_reads_as_a_reminder() -> None:
    message = notifications.render(_job(NotificationType.APPOINTMENT_REMINDER))

    assert "reminder" in message.subject.lower()


def test_a_patient_cancellation_does_not_apologise_to_them() -> None:
    """Telling someone the clinic is sorry for a cancellation they made would be absurd."""
    message = notifications.render(_job(NotificationType.CANCELLATION, cancelled_by_clinic=False))

    assert "clinic has had to cancel" not in message.body
    assert "has been cancelled" in message.body


def test_a_cancellation_reason_is_included_when_given() -> None:
    message = notifications.render(
        _job(NotificationType.CANCELLATION, cancelled_by_clinic=True, reason="Doctor unwell")
    )

    assert "Doctor unwell" in message.body


def test_rendering_survives_a_payload_missing_optional_fields() -> None:
    """A template crash would turn one bad row into a stuck queue."""
    bare = NotificationJob(
        notification_type=NotificationType.BOOKING_CONFIRMATION,
        status=NotificationStatus.PENDING,
        recipient_email="someone@example.com",
        payload={},
        scheduled_for=datetime.now(UTC),
    )

    message = notifications.render(bare)

    assert message.subject
    assert message.body


def test_local_formatting_uses_the_clinic_zone() -> None:
    moment = datetime(2026, 9, 3, 11, 0, tzinfo=UTC)

    assert "16:30" in notifications.format_local(moment, ZoneInfo("Asia/Kolkata"))
