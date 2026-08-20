"""Admin visibility into the notification outbox.

A message that exhausted its retries is parked rather than deleted, precisely so it can be
seen here. Without this view "the email never arrived" is unanswerable — there would be no
difference between a message that failed and one that was never raised.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, require_roles
from app.models.enums import NotificationStatus, NotificationType, UserRole
from app.models.notification import NotificationJob
from app.schemas.notification import NotificationJobResponse, NotificationSummaryResponse

router = APIRouter(
    prefix="/admin/notifications",
    tags=["admin: notifications"],
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)


def _to_response(job: NotificationJob) -> NotificationJobResponse:
    return NotificationJobResponse(
        id=job.id,
        notification_type=job.notification_type,
        status=job.status,
        recipient_email=job.recipient_email,
        appointment_id=job.appointment_id,
        scheduled_for=job.scheduled_for,
        attempts=job.attempts,
        next_attempt_at=job.next_attempt_at,
        sent_at=job.sent_at,
        last_error=job.last_error,
    )


@router.get("", response_model=list[NotificationJobResponse], summary="List queued messages")
async def list_notifications(
    session: AsyncSession = Depends(get_session),
    job_status: NotificationStatus | None = Query(
        default=None, alias="status", description="Filter by delivery state."
    ),
    notification_type: NotificationType | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[NotificationJobResponse]:
    query = select(NotificationJob)
    if job_status is not None:
        query = query.where(NotificationJob.status == job_status)
    if notification_type is not None:
        query = query.where(NotificationJob.notification_type == notification_type)

    result = await session.execute(query.order_by(NotificationJob.created_at.desc()).limit(limit))
    return [_to_response(job) for job in result.scalars().all()]


@router.get("/summary", response_model=NotificationSummaryResponse, summary="Delivery health")
async def notification_summary(
    session: AsyncSession = Depends(get_session),
) -> NotificationSummaryResponse:
    """Counts by state — the one number an admin actually watches is `failed`."""
    counts: dict[NotificationStatus, int] = {}
    for state in NotificationStatus:
        result = await session.execute(
            select(NotificationJob.id).where(NotificationJob.status == state)
        )
        counts[state] = len(result.scalars().all())

    return NotificationSummaryResponse(
        pending=counts[NotificationStatus.PENDING],
        sent=counts[NotificationStatus.SENT],
        failed=counts[NotificationStatus.FAILED],
    )


@router.post(
    "/{job_id}/retry",
    response_model=NotificationJobResponse,
    summary="Requeue a failed message",
)
async def retry_notification(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> NotificationJobResponse:
    """Put a dead-lettered message back in the queue.

    The attempt count is reset so it gets a full retry budget again — the usual reason to
    press this is that the underlying problem (a bad API key, a provider outage) has been
    fixed, and the previous failures are no longer informative.
    """
    job = await session.get(NotificationJob, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No notification with id {job_id}."
        )
    if job.status is not NotificationStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only a failed notification can be retried; this one is {job.status.value}.",
        )

    job.status = NotificationStatus.PENDING
    job.attempts = 0
    job.next_attempt_at = None
    job.last_error = None
    await session.commit()
    await session.refresh(job)

    return _to_response(job)
