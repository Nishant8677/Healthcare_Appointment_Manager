"""Patient-facing doctor search and slot availability.

Read-only, and available to any signed-in user. Nothing here exposes a doctor's contact
details or patient list — only what a patient needs in order to choose an appointment.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_app_settings, get_current_user, get_session
from app.core.config import Settings
from app.core.exceptions import DoctorNotFound
from app.models.doctor import DoctorProfile
from app.models.user import User
from app.schemas.appointment import AvailabilityResponse, DoctorSummary, SlotResponse
from app.services import availability, doctor_service

router = APIRouter(
    prefix="/doctors",
    tags=["doctors"],
    dependencies=[Depends(get_current_user)],
)


def _to_summary(profile: DoctorProfile) -> DoctorSummary:
    return DoctorSummary(
        id=profile.id,
        full_name=profile.user.full_name,
        specialisation=profile.specialisation,
        slot_duration_minutes=profile.slot_duration_minutes,
    )


@router.get("", response_model=list[DoctorSummary], summary="Search doctors")
async def search_doctors(
    session: AsyncSession = Depends(get_session),
    specialisation: str | None = Query(
        default=None, description="Case-insensitive substring, e.g. 'cardio'."
    ),
) -> list[DoctorSummary]:
    """Find doctors a patient can book with.

    Deactivated doctors are never listed here — unlike the admin view, a patient has no
    reason to see someone they cannot book.
    """
    profiles = await doctor_service.list_doctors(session, specialisation=specialisation)
    return [_to_summary(profile) for profile in profiles]


@router.get(
    "/{doctor_id}/slots",
    response_model=AvailabilityResponse,
    summary="Free appointment slots on a date",
)
async def doctor_slots(
    doctor_id: uuid.UUID,
    day: date = Query(alias="date", description="Calendar date in the clinic's timezone."),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
    _: User = Depends(get_current_user),
) -> AvailabilityResponse:
    """Slots are computed from the doctor's working hours, minus leave, minus bookings.

    Nothing is stored: a change to a doctor's schedule is reflected immediately, with no
    regeneration step and no stale rows.
    """
    try:
        doctor = await doctor_service.get_doctor(session, doctor_id)
    except DoctorNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No doctor with id {doctor_id}."
        ) from None

    slots = await availability.available_slots(session, doctor, day, zone=settings.clinic_zone)

    return AvailabilityResponse(
        doctor_id=doctor.id,
        date=day,
        slot_duration_minutes=doctor.slot_duration_minutes,
        timezone=settings.clinic_timezone,
        slots=[SlotResponse(starts_at=slot.starts_at, ends_at=slot.ends_at) for slot in slots],
    )
