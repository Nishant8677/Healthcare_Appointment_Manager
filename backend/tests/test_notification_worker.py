"""The outbox worker: delivery, retries, backoff and dead-lettering.

The reliability requirement is that a message is never lost when a provider is down. These
tests make the provider fail on demand and assert what survives.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import Database
from app.models.enums import NotificationStatus, NotificationType
from app.models.notification import NotificationJob
from app.services.email import EmailDeliveryError, EmailMessage
from app.workers.notification_worker import deliver_once
from app.workers.runner import backoff_for

NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


class RecordingSender:
    """Collects what it was asked to send, and fails on command."""

    def __init__(self, *, fail_times: int = 0, fail_forever: bool = False) -> None:
        self.sent: list[EmailMessage] = []
        self.attempts = 0
        self._fail_times = fail_times
        self._fail_forever = fail_forever

    async def send(self, message: EmailMessage) -> None:
        self.attempts += 1
        if self._fail_forever or self.attempts <= self._fail_times:
            raise EmailDeliveryError("provider unavailable")
        self.sent.append(message)


def make_job(
    *,
    scheduled_for: datetime = NOW,
    status: NotificationStatus = NotificationStatus.PENDING,
    attempts: int = 0,
    next_attempt_at: datetime | None = None,
    email: str = "patient@example.com",
) -> NotificationJob:
    return NotificationJob(
        notification_type=NotificationType.BOOKING_CONFIRMATION,
        status=status,
        recipient_email=email,
        payload={"recipient_name": "Meera Nair", "starts_at_local": "Tuesday at 09:00"},
        scheduled_for=scheduled_for,
        attempts=attempts,
        next_attempt_at=next_attempt_at,
    )


async def reload_job(session: AsyncSession, job_id: object) -> NotificationJob:
    session.expire_all()
    result = await session.execute(select(NotificationJob).where(NotificationJob.id == job_id))
    return result.scalar_one()


# ---------------------------------------------------------------- happy path


async def test_a_due_message_is_sent_and_marked(db_session: AsyncSession) -> None:
    job = make_job()
    db_session.add(job)
    await db_session.commit()
    sender = RecordingSender()

    report = await deliver_once(db_session, sender, now=NOW)

    assert report.sent == 1
    assert len(sender.sent) == 1
    stored = await reload_job(db_session, job.id)
    assert stored.status is NotificationStatus.SENT
    assert stored.sent_at == NOW
    assert stored.attempts == 1


async def test_a_message_that_is_not_due_yet_is_left_alone(db_session: AsyncSession) -> None:
    """This is how reminders stay dormant until their time."""
    job = make_job(scheduled_for=NOW + timedelta(hours=5))
    db_session.add(job)
    await db_session.commit()
    sender = RecordingSender()

    report = await deliver_once(db_session, sender, now=NOW)

    assert report.processed == 0
    assert sender.sent == []
    assert (await reload_job(db_session, job.id)).status is NotificationStatus.PENDING


async def test_an_already_sent_message_is_not_sent_again(db_session: AsyncSession) -> None:
    job = make_job(status=NotificationStatus.SENT)
    job.sent_at = NOW - timedelta(hours=1)
    db_session.add(job)
    await db_session.commit()

    report = await deliver_once(db_session, RecordingSender(), now=NOW)

    assert report.processed == 0


# ---------------------------------------------------------------- failure handling


async def test_a_failed_send_is_kept_and_scheduled_for_retry(db_session: AsyncSession) -> None:
    """The message must survive the provider having a bad minute."""
    job = make_job()
    db_session.add(job)
    await db_session.commit()

    report = await deliver_once(db_session, RecordingSender(fail_forever=True), now=NOW)

    assert report.retried == 1
    stored = await reload_job(db_session, job.id)
    assert stored.status is NotificationStatus.PENDING
    assert stored.attempts == 1
    assert stored.next_attempt_at == NOW + timedelta(minutes=1)
    assert stored.last_error is not None


async def test_a_job_waiting_on_backoff_is_skipped_until_its_time(
    db_session: AsyncSession,
) -> None:
    job = make_job(attempts=1, next_attempt_at=NOW + timedelta(minutes=1))
    db_session.add(job)
    await db_session.commit()
    sender = RecordingSender()

    too_early = await deliver_once(db_session, sender, now=NOW)
    on_time = await deliver_once(db_session, sender, now=NOW + timedelta(minutes=2))

    assert too_early.processed == 0
    assert on_time.sent == 1


async def test_delivery_succeeds_on_a_later_attempt(db_session: AsyncSession) -> None:
    """The whole point of the outbox: a transient outage delays a message, never loses it."""
    job = make_job()
    db_session.add(job)
    await db_session.commit()
    sender = RecordingSender(fail_times=2)

    first = await deliver_once(db_session, sender, now=NOW)
    second = await deliver_once(db_session, sender, now=NOW + timedelta(minutes=2))
    third = await deliver_once(db_session, sender, now=NOW + timedelta(minutes=10))

    assert (first.retried, second.retried, third.sent) == (1, 1, 1)
    stored = await reload_job(db_session, job.id)
    assert stored.status is NotificationStatus.SENT
    assert stored.attempts == 3
    assert stored.last_error is None


async def test_a_message_is_parked_after_exhausting_its_retries(
    db_session: AsyncSession,
) -> None:
    """Parked rather than deleted, so the admin view can show it. A vanished message would be
    indistinguishable from one never raised."""
    job = make_job()
    db_session.add(job)
    await db_session.commit()
    sender = RecordingSender(fail_forever=True)

    moment = NOW
    for _ in range(4):
        await deliver_once(db_session, sender, now=moment, max_attempts=4)
        moment += timedelta(hours=1)

    stored = await reload_job(db_session, job.id)
    assert stored.status is NotificationStatus.FAILED
    assert stored.attempts == 4
    assert "provider unavailable" in (stored.last_error or "")


async def test_one_bad_message_does_not_block_the_others(db_session: AsyncSession) -> None:
    """A single poisonous row must not stop the queue for everyone else."""

    class SelectiveSender:
        def __init__(self) -> None:
            self.sent: list[EmailMessage] = []

        async def send(self, message: EmailMessage) -> None:
            if message.to_address == "broken@example.com":
                raise EmailDeliveryError("rejected")
            self.sent.append(message)

    db_session.add(make_job(email="broken@example.com"))
    db_session.add(make_job(email="fine-one@example.com"))
    db_session.add(make_job(email="fine-two@example.com"))
    await db_session.commit()
    sender = SelectiveSender()

    report = await deliver_once(db_session, sender, now=NOW)

    assert report.sent == 2
    assert report.retried == 1
    assert sorted(m.to_address for m in sender.sent) == [
        "fine-one@example.com",
        "fine-two@example.com",
    ]


async def test_an_unexpected_error_is_retried_rather_than_crashing_the_batch(
    db_session: AsyncSession,
) -> None:
    """A template bug should delay one message, not take the worker down."""

    class ExplodingSender:
        async def send(self, message: EmailMessage) -> None:
            raise RuntimeError("something entirely unexpected")

    job = make_job()
    db_session.add(job)
    await db_session.commit()

    report = await deliver_once(db_session, ExplodingSender(), now=NOW)

    assert report.retried == 1
    stored = await reload_job(db_session, job.id)
    assert stored.status is NotificationStatus.PENDING
    assert "unexpected error" in (stored.last_error or "")


# ---------------------------------------------------------------- backoff shape


@pytest.mark.parametrize(
    ("attempt", "expected_minutes"),
    [(1, 1), (2, 5), (3, 30), (4, 30), (9, 30)],
)
def test_backoff_grows_then_holds(attempt: int, expected_minutes: int) -> None:
    assert backoff_for(attempt) == timedelta(minutes=expected_minutes)


# ---------------------------------------------------------------- concurrency


async def test_two_workers_never_send_the_same_message_twice(
    db_session: AsyncSession, database: Database, settings: object
) -> None:
    """`FOR UPDATE SKIP LOCKED` is what lets the API be scaled to more than one instance."""
    for index in range(10):
        db_session.add(make_job(email=f"patient-{index}@example.com"))
    await db_session.commit()

    sender_a, sender_b = RecordingSender(), RecordingSender()
    session_a, session_b = database.session(), database.session()

    try:
        report_a, report_b = await asyncio.gather(
            deliver_once(session_a, sender_a, now=NOW, limit=10),
            deliver_once(session_b, sender_b, now=NOW, limit=10),
        )
    finally:
        await session_a.close()
        await session_b.close()

    delivered = [m.to_address for m in sender_a.sent] + [m.to_address for m in sender_b.sent]

    assert report_a.sent + report_b.sent == 10
    assert len(delivered) == len(set(delivered)) == 10, "a message was delivered twice"
