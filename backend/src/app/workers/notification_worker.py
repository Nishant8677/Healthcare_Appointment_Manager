"""The outbox worker: delivers queued notifications, retries the ones that fail.

Deliberately a plain polling loop rather than a scheduler library or a message broker. Every
job already carries `scheduled_for`, so "run due work" is one query — a broker would add an
operational dependency and, worse, would break the atomicity the outbox exists for: an enqueue
to a broker can succeed while the surrounding transaction rolls back.

Concurrency safety comes from `FOR UPDATE SKIP LOCKED`. Two instances of the API polling the
same table will hand each other different rows rather than both sending the same email, so
the service can be scaled out without a leader election or a distributed lock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import NotificationStatus
from app.models.notification import NotificationJob
from app.services import notifications
from app.services.email import EmailDeliveryError, EmailSender

logger = logging.getLogger(__name__)

# Growing gaps: a provider having a bad second deserves a quick retry, one having a bad hour
# does not deserve to be hammered. The last entry repeats for any further attempts.
RETRY_BACKOFF = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
)


def backoff_for(attempts: int) -> timedelta:
    """Delay before retry number `attempts` (1-based)."""
    index = min(max(attempts, 1), len(RETRY_BACKOFF)) - 1
    return RETRY_BACKOFF[index]


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    """What one pass of the worker did. Returned so tests can assert on it directly."""

    sent: int = 0
    retried: int = 0
    failed: int = 0

    @property
    def processed(self) -> int:
        return self.sent + self.retried + self.failed


async def claim_due_jobs(
    session: AsyncSession, *, now: datetime, limit: int
) -> list[NotificationJob]:
    """Take up to `limit` jobs that are due, locking them for this transaction.

    `SKIP LOCKED` is what makes a second worker safe: rows already claimed elsewhere are
    passed over instead of blocking, so no message is ever delivered twice.
    """
    query = (
        select(NotificationJob)
        .where(
            NotificationJob.status == NotificationStatus.PENDING,
            NotificationJob.scheduled_for <= now,
            or_(
                NotificationJob.next_attempt_at.is_(None),
                NotificationJob.next_attempt_at <= now,
            ),
        )
        .order_by(NotificationJob.scheduled_for)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(query)
    return list(result.scalars().all())


async def deliver_once(
    session: AsyncSession,
    sender: EmailSender,
    *,
    now: datetime | None = None,
    limit: int = 20,
    max_attempts: int = 4,
) -> DeliveryReport:
    """Claim the due jobs and try to deliver each one.

    The whole batch commits together at the end: the row locks must be held while sending,
    otherwise another worker could claim a job mid-flight.
    """
    reference = now or datetime.now(UTC)
    jobs = await claim_due_jobs(session, now=reference, limit=limit)
    if not jobs:
        return DeliveryReport()

    sent = retried = failed = 0

    for job in jobs:
        job.attempts += 1
        try:
            await sender.send(notifications.render(job))
        except EmailDeliveryError as error:
            if job.attempts >= max_attempts:
                # Parked rather than deleted: the admin view surfaces these, and a message
                # that silently vanished would be indistinguishable from one never raised.
                job.status = NotificationStatus.FAILED
                job.last_error = str(error)[:1000]
                failed += 1
                logger.error(
                    "notification permanently failed",
                    extra={"job_id": str(job.id), "attempts": job.attempts},
                )
            else:
                job.next_attempt_at = reference + backoff_for(job.attempts)
                job.last_error = str(error)[:1000]
                retried += 1
                logger.warning(
                    "notification delivery failed, will retry",
                    extra={
                        "job_id": str(job.id),
                        "attempts": job.attempts,
                        "next_attempt_at": job.next_attempt_at.isoformat(),
                    },
                )
        except Exception as error:
            # An unexpected error (a template bug, say) is treated like a delivery failure so
            # the remaining jobs in the batch still go out.
            job.next_attempt_at = reference + backoff_for(job.attempts)
            job.last_error = f"unexpected error: {error}"[:1000]
            retried += 1
            logger.exception(
                "notification raised an unexpected error", extra={"job_id": str(job.id)}
            )
        else:
            job.status = NotificationStatus.SENT
            job.sent_at = reference
            job.last_error = None
            sent += 1

    await session.commit()
    return DeliveryReport(sent=sent, retried=retried, failed=failed)
