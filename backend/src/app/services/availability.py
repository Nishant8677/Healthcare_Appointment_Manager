"""Working out which appointment slots a doctor actually has free.

Slots are computed on demand rather than stored as rows. Materialising a year of empty slots
per doctor would mean a background job to extend them, a migration whenever hours change, and
a large table that is almost entirely "nothing booked here". Deriving them from working hours
keeps a schedule change instantly and retroactively correct.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import Appointment
from app.models.doctor import DoctorProfile
from app.models.enums import AppointmentStatus
from app.services.scheduling import WorkingWindow, combine_in_zone, slot_starts_for_weekday


@dataclass(frozen=True, slots=True)
class Slot:
    """One bookable appointment, as UTC instants."""

    starts_at: datetime
    ends_at: datetime


def windows_of(doctor: DoctorProfile) -> list[WorkingWindow]:
    """The doctor's weekly availability as domain values."""
    return [
        WorkingWindow(weekday=row.weekday, start=row.start_time, end=row.end_time)
        for row in doctor.working_hours
    ]


def candidate_slots(doctor: DoctorProfile, day: date, *, zone: ZoneInfo) -> list[Slot]:
    """Every slot the doctor's schedule defines for that day, before checking bookings.

    Returns nothing for a leave day or a day outside their working pattern. Pure apart from
    reading already-loaded relationships.
    """
    if day in {leave.leave_date for leave in doctor.leave_days}:
        return []

    duration = timedelta(minutes=doctor.slot_duration_minutes)
    slots: list[Slot] = []

    for local_time in slot_starts_for_weekday(
        windows_of(doctor), doctor.slot_duration_minutes, day.weekday()
    ):
        starts_at = combine_in_zone(day, local_time, zone)
        if starts_at is None:
            # A local time that does not exist on this date (daylight-saving gap).
            continue
        slots.append(Slot(starts_at=starts_at, ends_at=starts_at + duration))

    return slots


async def occupied_starts(
    session: AsyncSession,
    doctor_id: uuid.UUID,
    starts: Sequence[datetime],
    *,
    now: datetime,
) -> set[datetime]:
    """Which of these start times are already taken.

    A hold counts as occupied only while it is still live: an abandoned hold stops blocking
    the slot the moment it expires, which is why no sweeper job is needed to reclaim them.
    """
    if not starts:
        return set()

    query = select(Appointment.starts_at).where(
        Appointment.doctor_profile_id == doctor_id,
        Appointment.starts_at.in_(list(starts)),
        or_(
            Appointment.status.in_([AppointmentStatus.CONFIRMED, AppointmentStatus.COMPLETED]),
            (Appointment.status == AppointmentStatus.HELD) & (Appointment.hold_expires_at > now),
        ),
    )
    result = await session.execute(query)
    return set(result.scalars().all())


async def available_slots(
    session: AsyncSession,
    doctor: DoctorProfile,
    day: date,
    *,
    zone: ZoneInfo,
    now: datetime | None = None,
) -> list[Slot]:
    """The doctor's free slots on a given date, in order.

    `now` is injectable so "slots in the past are not offered" can be tested against a fixed
    clock instead of whatever time the suite happens to run at.
    """
    reference = now or datetime.now(UTC)

    if not doctor.user.is_active:
        return []

    # A slot that has already started cannot be booked, so it is never offered.
    upcoming = [
        slot for slot in candidate_slots(doctor, day, zone=zone) if slot.starts_at > reference
    ]
    taken = await occupied_starts(
        session, doctor.id, [slot.starts_at for slot in upcoming], now=reference
    )

    return [slot for slot in upcoming if slot.starts_at not in taken]
