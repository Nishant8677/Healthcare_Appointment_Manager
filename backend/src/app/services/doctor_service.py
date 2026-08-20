"""Admin management of doctors: profiles, weekly availability and leave.

Schedule *rules* live in `app.services.scheduling` as pure functions; this module is the
imperative shell that loads rows, applies those rules, and writes the result.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import DoctorNotFound, EmailAlreadyRegistered
from app.core.security import hash_password
from app.models.doctor import DoctorProfile, DoctorWorkingHours
from app.models.enums import UserRole
from app.models.user import User
from app.services.auth_service import normalise_email
from app.services.scheduling import WorkingWindow, validate_weekly_schedule

logger = logging.getLogger(__name__)


def _with_relations() -> tuple[object, ...]:
    """Eager-load everything a doctor response needs.

    Required, not an optimisation: under async SQLAlchemy, touching an unloaded relationship
    outside the awaiting context raises rather than lazily querying.
    """
    return (
        selectinload(DoctorProfile.user),
        selectinload(DoctorProfile.working_hours),
        selectinload(DoctorProfile.leave_days),
    )


async def get_doctor(session: AsyncSession, doctor_id: uuid.UUID) -> DoctorProfile:
    """Load one doctor with their schedule and leave.

    Raises:
        DoctorNotFound: no profile with that id.
    """
    result = await session.execute(
        select(DoctorProfile).options(*_with_relations()).where(DoctorProfile.id == doctor_id)  # type: ignore[arg-type]
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        raise DoctorNotFound(str(doctor_id))
    return profile


async def list_doctors(
    session: AsyncSession,
    *,
    specialisation: str | None = None,
    include_inactive: bool = False,
) -> Sequence[DoctorProfile]:
    """List doctors, most recently added first."""
    query = select(DoctorProfile).options(*_with_relations()).join(DoctorProfile.user)  # type: ignore[arg-type]

    if specialisation:
        # Case-insensitive contains, so "cardio" finds "Cardiology".
        query = query.where(DoctorProfile.specialisation.ilike(f"%{specialisation}%"))
    if not include_inactive:
        query = query.where(User.is_active.is_(True))

    result = await session.execute(query.order_by(DoctorProfile.created_at.desc()))
    return result.scalars().all()


async def create_doctor(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    full_name: str,
    specialisation: str,
    slot_duration_minutes: int,
    working_hours: Sequence[WorkingWindow] = (),
) -> DoctorProfile:
    """Create a doctor's login and clinic profile in one transaction.

    Raises:
        InvalidSchedule: the supplied working hours are inconsistent.
        EmailAlreadyRegistered: the address is taken.
    """
    # Validate before touching the database: a rejected schedule should cost nothing.
    validate_weekly_schedule(working_hours, slot_duration_minutes)

    user = User(
        email=normalise_email(email),
        password_hash=hash_password(password),
        full_name=full_name,
        role=UserRole.DOCTOR,
    )
    profile = DoctorProfile(
        user=user,
        specialisation=specialisation,
        slot_duration_minutes=slot_duration_minutes,
        working_hours=[
            DoctorWorkingHours(weekday=window.weekday, start_time=window.start, end_time=window.end)
            for window in working_hours
        ],
    )
    session.add(profile)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise EmailAlreadyRegistered(email) from exc

    logger.info("doctor created", extra={"doctor_profile_id": str(profile.id)})
    return await get_doctor(session, profile.id)


async def update_doctor(
    session: AsyncSession,
    doctor_id: uuid.UUID,
    *,
    specialisation: str | None = None,
    slot_duration_minutes: int | None = None,
    full_name: str | None = None,
    is_active: bool | None = None,
) -> DoctorProfile:
    """Apply a partial update.

    Changing the slot duration re-validates the existing weekly schedule: a duration that no
    longer divides the doctor's working windows would silently produce unbookable gaps, so
    it is rejected rather than stored.
    """
    profile = await get_doctor(session, doctor_id)

    if slot_duration_minutes is not None and slot_duration_minutes != profile.slot_duration_minutes:
        validate_weekly_schedule(_windows_of(profile), slot_duration_minutes)
        profile.slot_duration_minutes = slot_duration_minutes

    if specialisation is not None:
        profile.specialisation = specialisation
    if full_name is not None:
        profile.user.full_name = full_name
    if is_active is not None:
        profile.user.is_active = is_active

    await session.commit()
    logger.info("doctor updated", extra={"doctor_profile_id": str(doctor_id)})
    return await get_doctor(session, doctor_id)


async def replace_working_hours(
    session: AsyncSession,
    doctor_id: uuid.UUID,
    windows: Sequence[WorkingWindow],
) -> DoctorProfile:
    """Replace the doctor's entire weekly availability.

    Raises:
        InvalidSchedule: windows overlap, or do not divide into whole appointments.
    """
    profile = await get_doctor(session, doctor_id)
    validate_weekly_schedule(windows, profile.slot_duration_minutes)

    # `delete-orphan` on the relationship removes the previous rows when they leave the
    # collection, so the replacement is a single atomic swap.
    profile.working_hours.clear()
    profile.working_hours.extend(
        DoctorWorkingHours(weekday=window.weekday, start_time=window.start, end_time=window.end)
        for window in windows
    )

    await session.commit()
    logger.info(
        "working hours replaced",
        extra={"doctor_profile_id": str(doctor_id), "window_count": len(windows)},
    )
    return await get_doctor(session, doctor_id)


def _windows_of(profile: DoctorProfile) -> list[WorkingWindow]:
    return [
        WorkingWindow(weekday=row.weekday, start=row.start_time, end=row.end_time)
        for row in profile.working_hours
    ]
