"""Admin visibility into the outbox: seeing what failed, and putting it back."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import NotificationStatus, NotificationType
from app.models.notification import NotificationJob

Headers = dict[str, str]

BASE = "/admin/notifications"


def make_job(
    *,
    status: NotificationStatus = NotificationStatus.PENDING,
    attempts: int = 0,
    last_error: str | None = None,
) -> NotificationJob:
    job = NotificationJob(
        notification_type=NotificationType.BOOKING_CONFIRMATION,
        status=status,
        recipient_email="patient@example.com",
        payload={"recipient_name": "Meera Nair"},
        scheduled_for=datetime.now(UTC),
        attempts=attempts,
        last_error=last_error,
    )
    if status is NotificationStatus.SENT:
        job.sent_at = datetime.now(UTC)
    return job


async def test_admin_can_see_failed_messages(
    client: AsyncClient, admin_headers: Headers, db_session: AsyncSession
) -> None:
    """Without this, "the email never arrived" is unanswerable."""
    db_session.add(make_job(status=NotificationStatus.SENT))
    db_session.add(
        make_job(status=NotificationStatus.FAILED, attempts=4, last_error="provider unavailable")
    )
    await db_session.commit()

    response = await client.get(BASE, headers=admin_headers, params={"status": "failed"})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["attempts"] == 4
    assert body[0]["last_error"] == "provider unavailable"


async def test_the_summary_counts_each_state(
    client: AsyncClient, admin_headers: Headers, db_session: AsyncSession
) -> None:
    db_session.add(make_job(status=NotificationStatus.PENDING))
    db_session.add(make_job(status=NotificationStatus.SENT))
    db_session.add(make_job(status=NotificationStatus.SENT))
    db_session.add(make_job(status=NotificationStatus.FAILED, attempts=4))
    await db_session.commit()

    response = await client.get(f"{BASE}/summary", headers=admin_headers)

    assert response.json() == {"pending": 1, "sent": 2, "failed": 1}


async def test_a_failed_message_can_be_requeued(
    client: AsyncClient, admin_headers: Headers, db_session: AsyncSession
) -> None:
    """After the underlying problem is fixed, the message should get a fresh budget."""
    job = make_job(status=NotificationStatus.FAILED, attempts=4, last_error="bad api key")
    db_session.add(job)
    await db_session.commit()

    response = await client.post(f"{BASE}/{job.id}/retry", headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["attempts"] == 0
    assert body["last_error"] is None


async def test_only_a_failed_message_can_be_requeued(
    client: AsyncClient, admin_headers: Headers, db_session: AsyncSession
) -> None:
    """Requeuing a delivered message would send it a second time."""
    job = make_job(status=NotificationStatus.SENT)
    db_session.add(job)
    await db_session.commit()

    response = await client.post(f"{BASE}/{job.id}/retry", headers=admin_headers)

    assert response.status_code == 409


async def test_requeuing_an_unknown_message_is_not_found(
    client: AsyncClient, admin_headers: Headers
) -> None:
    response = await client.post(f"{BASE}/{uuid.uuid4()}/retry", headers=admin_headers)

    assert response.status_code == 404


async def test_patients_cannot_read_the_outbox(
    client: AsyncClient, patient_headers: Headers, db_session: AsyncSession
) -> None:
    """The queue carries other patients' names and appointment times."""
    db_session.add(make_job())
    await db_session.commit()

    response = await client.get(BASE, headers=patient_headers)

    assert response.status_code == 403
