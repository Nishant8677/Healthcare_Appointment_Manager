"""Admin visibility into calendar sync.

Same reasoning as the notification view: a calendar entry that never appeared is otherwise
indistinguishable from one that was never requested. The distinction that matters here is
`skipped` versus `failed` — the first means the user simply has no calendar connected, which
is the normal state and not something anyone should be paged about.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, require_roles
from app.models.calendar import CalendarSyncJob
from app.models.enums import CalendarSyncStatus, UserRole
from app.schemas.calendar import CalendarSyncJobResponse, CalendarSyncSummaryResponse

router = APIRouter(
    prefix="/admin/calendar",
    tags=["admin: calendar"],
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)


@router.get(
    "/sync-jobs",
    response_model=list[CalendarSyncJobResponse],
    summary="List calendar sync state",
)
async def list_sync_jobs(
    session: AsyncSession = Depends(get_session),
    job_status: CalendarSyncStatus | None = Query(
        default=None, alias="status", description="Filter by reconciliation state."
    ),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[CalendarSyncJobResponse]:
    query = select(CalendarSyncJob)
    if job_status is not None:
        query = query.where(CalendarSyncJob.status == job_status)
    result = await session.execute(query.order_by(CalendarSyncJob.created_at.desc()).limit(limit))
    return [CalendarSyncJobResponse.model_validate(job) for job in result.scalars().all()]


@router.get(
    "/summary",
    response_model=CalendarSyncSummaryResponse,
    summary="Calendar sync health",
)
async def sync_summary(session: AsyncSession = Depends(get_session)) -> CalendarSyncSummaryResponse:
    """Counts by state, from one grouped query rather than one query per state."""
    result = await session.execute(
        select(CalendarSyncJob.status, func.count()).group_by(CalendarSyncJob.status)
    )
    counts = dict.fromkeys(CalendarSyncStatus, 0)
    for state, total in result.all():
        counts[state] = total

    return CalendarSyncSummaryResponse(
        pending=counts[CalendarSyncStatus.PENDING],
        synced=counts[CalendarSyncStatus.SYNCED],
        skipped=counts[CalendarSyncStatus.SKIPPED],
        failed=counts[CalendarSyncStatus.FAILED],
    )


@router.post(
    "/sync-jobs/{job_id}/retry",
    response_model=CalendarSyncJobResponse,
    summary="Requeue a failed calendar entry",
)
async def retry_sync_job(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> CalendarSyncJobResponse:
    """Put a dead-lettered row back in the queue with a full retry budget.

    Also accepts `skipped` rows: the usual reason one is skipped is that the user had no
    calendar connected at the time, and pressing this after they connect is exactly right.
    """
    job = await session.get(CalendarSyncJob, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No calendar sync job with id {job_id}."
        )
    if job.status is CalendarSyncStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This calendar entry is already queued.",
        )

    job.status = CalendarSyncStatus.PENDING
    job.attempts = 0
    job.next_attempt_at = None
    job.last_error = None
    await session.commit()
    await session.refresh(job)

    return CalendarSyncJobResponse.model_validate(job)
