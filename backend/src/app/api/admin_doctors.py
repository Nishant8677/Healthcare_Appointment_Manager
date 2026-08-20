"""Admin endpoints for managing doctors, their weekly availability and their leave.

Every route here is admin-only. The guard is declared once on the router rather than repeated
per route, so a new endpoint cannot accidentally ship unprotected.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, require_roles
from app.core.exceptions import (
    DoctorNotFound,
    DuplicateLeaveDay,
    EmailAlreadyRegistered,
    InvalidSchedule,
    LeaveDayNotFound,
)
from app.models.doctor import DoctorProfile
from app.models.enums import UserRole
from app.schemas.doctor import (
    DoctorCreateRequest,
    DoctorDetailResponse,
    DoctorResponse,
    DoctorUpdateRequest,
    LeaveDayCreateRequest,
    LeaveDayResponse,
    WorkingHoursItem,
    WorkingHoursReplaceRequest,
    WorkingHoursResponse,
)
from app.services import doctor_service
from app.services.scheduling import WorkingWindow

router = APIRouter(
    prefix="/admin/doctors",
    tags=["admin: doctors"],
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)


def _to_windows(items: list[WorkingHoursItem]) -> list[WorkingWindow]:
    """Translate the HTTP contract into the domain type the rules operate on."""
    return [
        WorkingWindow(weekday=item.weekday, start=item.start_time, end=item.end_time)
        for item in items
    ]


def _to_detail(profile: DoctorProfile) -> DoctorDetailResponse:
    """Flatten the profile and its user account into one response."""
    return DoctorDetailResponse(
        id=profile.id,
        user_id=profile.user_id,
        full_name=profile.user.full_name,
        email=profile.user.email,
        specialisation=profile.specialisation,
        slot_duration_minutes=profile.slot_duration_minutes,
        is_active=profile.user.is_active,
        working_hours=[
            WorkingHoursResponse(
                id=row.id,
                weekday=row.weekday,
                start_time=row.start_time,
                end_time=row.end_time,
            )
            for row in profile.working_hours
        ],
        leave_days=[
            LeaveDayResponse(id=row.id, leave_date=row.leave_date, reason=row.reason)
            for row in profile.leave_days
        ],
    )


def _to_summary(profile: DoctorProfile) -> DoctorResponse:
    return DoctorResponse(
        id=profile.id,
        user_id=profile.user_id,
        full_name=profile.user.full_name,
        email=profile.user.email,
        specialisation=profile.specialisation,
        slot_duration_minutes=profile.slot_duration_minutes,
        is_active=profile.user.is_active,
    )


def _not_found(doctor_id: uuid.UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"No doctor with id {doctor_id}.",
    )


def _unprocessable(error: InvalidSchedule) -> HTTPException:
    """A schedule the clinic cannot honour: valid JSON, impossible request."""
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error))


@router.post(
    "",
    response_model=DoctorDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a doctor",
)
async def create_doctor(
    payload: DoctorCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> DoctorDetailResponse:
    """Create the doctor's login and clinic profile together.

    Doctors cannot self-register, so this is the only way a doctor account comes into being.
    """
    try:
        profile = await doctor_service.create_doctor(
            session,
            email=payload.email,
            password=payload.password.get_secret_value(),
            full_name=payload.full_name,
            specialisation=payload.specialisation,
            slot_duration_minutes=payload.slot_duration_minutes,
            working_hours=_to_windows(payload.working_hours),
        )
    except InvalidSchedule as error:
        raise _unprocessable(error) from None
    except EmailAlreadyRegistered:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        ) from None

    return _to_detail(profile)


@router.get("", response_model=list[DoctorResponse], summary="List doctors")
async def list_doctors(
    session: AsyncSession = Depends(get_session),
    specialisation: str | None = Query(default=None, description="Case-insensitive substring."),
    include_inactive: bool = Query(default=False),
) -> list[DoctorResponse]:
    profiles = await doctor_service.list_doctors(
        session, specialisation=specialisation, include_inactive=include_inactive
    )
    return [_to_summary(profile) for profile in profiles]


@router.get("/{doctor_id}", response_model=DoctorDetailResponse, summary="Get one doctor")
async def get_doctor(
    doctor_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> DoctorDetailResponse:
    try:
        profile = await doctor_service.get_doctor(session, doctor_id)
    except DoctorNotFound:
        raise _not_found(doctor_id) from None
    return _to_detail(profile)


@router.patch("/{doctor_id}", response_model=DoctorDetailResponse, summary="Update a doctor")
async def update_doctor(
    doctor_id: uuid.UUID,
    payload: DoctorUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> DoctorDetailResponse:
    """Change specialisation, name, slot duration, or active status.

    Deactivating is preferred over deleting: appointments and prescriptions must remain
    attributable after a doctor leaves the clinic.
    """
    try:
        profile = await doctor_service.update_doctor(
            session,
            doctor_id,
            specialisation=payload.specialisation,
            slot_duration_minutes=payload.slot_duration_minutes,
            full_name=payload.full_name,
            is_active=payload.is_active,
        )
    except DoctorNotFound:
        raise _not_found(doctor_id) from None
    except InvalidSchedule as error:
        raise _unprocessable(error) from None

    return _to_detail(profile)


@router.put(
    "/{doctor_id}/working-hours",
    response_model=DoctorDetailResponse,
    summary="Replace the weekly schedule",
)
async def replace_working_hours(
    doctor_id: uuid.UUID,
    payload: WorkingHoursReplaceRequest,
    session: AsyncSession = Depends(get_session),
) -> DoctorDetailResponse:
    """Set the doctor's complete weekly availability.

    A full replacement rather than incremental edits: overlap can only be judged across the
    whole week, and replacing wholesale makes the request idempotent.
    """
    try:
        profile = await doctor_service.replace_working_hours(
            session, doctor_id, _to_windows(payload.working_hours)
        )
    except DoctorNotFound:
        raise _not_found(doctor_id) from None
    except InvalidSchedule as error:
        raise _unprocessable(error) from None

    return _to_detail(profile)


@router.post(
    "/{doctor_id}/leave",
    response_model=LeaveDayResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a leave day",
)
async def add_leave_day(
    doctor_id: uuid.UUID,
    payload: LeaveDayCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> LeaveDayResponse:
    try:
        leave = await doctor_service.add_leave_day(
            session,
            doctor_id,
            leave_date=payload.leave_date,
            reason=payload.reason,
        )
    except DoctorNotFound:
        raise _not_found(doctor_id) from None
    except InvalidSchedule as error:
        raise _unprocessable(error) from None
    except DuplicateLeaveDay:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{payload.leave_date.isoformat()} is already recorded as leave.",
        ) from None

    return LeaveDayResponse(id=leave.id, leave_date=leave.leave_date, reason=leave.reason)


@router.delete(
    "/{doctor_id}/leave/{leave_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a leave day",
)
async def remove_leave_day(
    doctor_id: uuid.UUID,
    leave_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        await doctor_service.remove_leave_day(session, doctor_id, leave_id)
    except LeaveDayNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No leave day {leave_id} for this doctor.",
        ) from None

    return Response(status_code=status.HTTP_204_NO_CONTENT)
