"""The calendar reconciler: makes Google agree with what the database says it should show.

One job per transaction, unlike the notification worker which processes a whole batch under
one commit. The difference is not stylistic. Notification rows are only ever *inserted* from
a request, so a batch-long row lock inconveniences nobody. Calendar rows are *updated* from a
request — cancelling an appointment rewrites its sync row — so holding twenty rows locked
across twenty round trips to Google would make a patient's cancellation wait on Google's
latency. Locking one row for one HTTP call keeps that wait bounded to a single request.

`SKIP LOCKED` still does the multi-instance work: two API instances polling this table hand
each other different rows instead of both writing the same event.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.calendar import CalendarSyncJob
from app.models.enums import CalendarSyncAction, CalendarSyncStatus
from app.services import calendar_sync
from app.services.google_calendar import (
    CalendarGateway,
    CalendarPermanentError,
    CalendarTransientError,
)
from app.services.google_oauth import GoogleAuthRevoked
from app.services.token_crypto import TokenCipher, TokenEncryptionError
from app.workers.runner import backoff_for

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncReport:
    """What one pass did. Returned so tests can assert on it without reading the table."""

    synced: int = 0
    deleted: int = 0
    skipped: int = 0
    retried: int = 0
    failed: int = 0

    @property
    def processed(self) -> int:
        return self.synced + self.deleted + self.skipped + self.retried + self.failed


async def claim_next_job(session: AsyncSession, *, now: datetime) -> CalendarSyncJob | None:
    """Lock the oldest due job for this transaction, or return `None` if there is none."""
    query = (
        select(CalendarSyncJob)
        .where(
            CalendarSyncJob.status == CalendarSyncStatus.PENDING,
            or_(
                CalendarSyncJob.next_attempt_at.is_(None),
                CalendarSyncJob.next_attempt_at <= now,
            ),
        )
        .order_by(CalendarSyncJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def sync_once(
    session: AsyncSession,
    gateway: CalendarGateway,
    cipher: TokenCipher | None,
    *,
    now: datetime | None = None,
    limit: int = 20,
    max_attempts: int = 5,
) -> SyncReport:
    """Reconcile up to `limit` calendars, committing after each one."""
    reference = now or datetime.now(UTC)
    synced = deleted = skipped = retried = failed = 0

    for _ in range(limit):
        job = await claim_next_job(session, now=reference)
        if job is None:
            break

        outcome = await _reconcile(
            session, job, gateway, cipher, reference=reference, max_attempts=max_attempts
        )
        # Committed per job so the row lock is released as soon as this one calendar is
        # settled, rather than at the end of the batch.
        await session.commit()

        match outcome:
            case "synced":
                synced += 1
            case "deleted":
                deleted += 1
            case "skipped":
                skipped += 1
            case "retried":
                retried += 1
            case _:
                failed += 1

    return SyncReport(
        synced=synced, deleted=deleted, skipped=skipped, retried=retried, failed=failed
    )


async def _reconcile(
    session: AsyncSession,
    job: CalendarSyncJob,
    gateway: CalendarGateway,
    cipher: TokenCipher | None,
    *,
    reference: datetime,
    max_attempts: int,
) -> str:
    """Bring one calendar in line with one job. Mutates `job`; the caller commits."""
    connection = await calendar_sync.get_connection(session, job.user_id)

    if connection is None or not connection.is_active:
        # Not a failure. The overwhelming majority of a clinic's patients will never connect
        # a calendar, and a user who disconnects is exercising a supported choice — neither
        # should show up in an admin's error count.
        return _skip(job, "no connected calendar for this user")

    if cipher is None:
        # Reachable only if the encryption key was removed after a connection was stored.
        return _skip(job, "calendar encryption key is not configured")

    try:
        refresh_token = cipher.decrypt(connection.encrypted_refresh_token)
    except TokenEncryptionError as error:
        # The stored token cannot be read, and no retry changes that. The connection is
        # marked so the portal tells the user to reconnect instead of silently doing nothing.
        connection.revoked_at = reference
        connection.last_error = str(error)[:1000]
        return _fail(job, str(error))

    # A delete for an event that was never created needs no network call at all — the
    # requested end state already holds.
    if job.action is CalendarSyncAction.DELETE and job.synced_at is None:
        job.status = CalendarSyncStatus.SYNCED
        job.last_error = None
        return "deleted"

    try:
        if job.action is CalendarSyncAction.SYNC:
            await gateway.upsert_event(
                refresh_token=refresh_token,
                calendar_id=job.calendar_id,
                event=calendar_sync.event_from_job(job),
                exists=job.synced_at is not None,
            )
        else:
            await gateway.delete_event(
                refresh_token=refresh_token,
                calendar_id=job.calendar_id,
                event_id=job.google_event_id,
            )
    except GoogleAuthRevoked as error:
        # The user withdrew access in their Google account. Retrying is pointless and the
        # connection is now dead, so both are recorded and the portal can offer a reconnect.
        connection.revoked_at = reference
        connection.last_error = str(error)[:1000]
        logger.info("calendar connection revoked at Google", extra={"user_id": str(job.user_id)})
        return _skip(job, "the calendar authorisation was revoked at Google")
    except CalendarTransientError as error:
        return _retry_or_fail(job, str(error), reference=reference, max_attempts=max_attempts)
    except CalendarPermanentError as error:
        logger.error(
            "calendar sync permanently rejected",
            extra={"job_id": str(job.id), "action": job.action.value},
        )
        return _fail(job, str(error))
    except Exception as error:
        # An unexpected error is treated as transient so one malformed row cannot stop the
        # rest of the queue, but it is logged with a traceback because it is a bug.
        logger.exception("calendar sync raised an unexpected error", extra={"job_id": str(job.id)})
        return _retry_or_fail(
            job, f"unexpected error: {error}", reference=reference, max_attempts=max_attempts
        )

    job.status = CalendarSyncStatus.SYNCED
    job.last_error = None
    if job.action is CalendarSyncAction.SYNC:
        job.synced_at = reference
        return "synced"

    # Google no longer holds the event, so a later re-sync of this row must insert rather
    # than update.
    job.synced_at = None
    return "deleted"


def _skip(job: CalendarSyncJob, reason: str) -> str:
    job.status = CalendarSyncStatus.SKIPPED
    job.last_error = reason
    return "skipped"


def _fail(job: CalendarSyncJob, reason: str) -> str:
    job.attempts += 1
    job.status = CalendarSyncStatus.FAILED
    job.last_error = reason[:1000]
    return "failed"


def _retry_or_fail(
    job: CalendarSyncJob, reason: str, *, reference: datetime, max_attempts: int
) -> str:
    job.attempts += 1
    job.last_error = reason[:1000]
    if job.attempts >= max_attempts:
        job.status = CalendarSyncStatus.FAILED
        logger.error(
            "calendar sync permanently failed",
            extra={"job_id": str(job.id), "attempts": job.attempts},
        )
        return "failed"
    job.next_attempt_at = reference + backoff_for(job.attempts)
    return "retried"
