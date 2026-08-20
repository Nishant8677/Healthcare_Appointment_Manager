"""Slot availability, tested at the service level against a real database.

Slots are derived from working hours rather than stored, so these tests are the proof that
the derivation subtracts the right things: leave, existing bookings, live holds, and the past.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Appointment, AppointmentStatus, DoctorProfile, User, UserRole
from app.models.doctor import DoctorLeaveDay
from app.services.availability import available_slots

MakeUser = Callable[..., Awaitable[User]]

UTC_ZONE = ZoneInfo("UTC")


def a_future_day() -> datetime:
    """A date far enough ahead that its 09:00 slot is never in the past mid-run."""
    return datetime.now(UTC) + timedelta(days=2)


def slot_at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day.date(), time(hour, minute), tzinfo=UTC)


async def _reload(session: AsyncSession, doctor: DoctorProfile) -> DoctorProfile:
    """Re-fetch with relations loaded, as the request path would."""
    from app.services.doctor_service import get_doctor

    return await get_doctor(session, doctor.id)


async def test_a_working_day_offers_every_slot(
    db_session: AsyncSession, bookable_doctor: DoctorProfile
) -> None:
    day = a_future_day()
    doctor = await _reload(db_session, bookable_doctor)

    slots = await available_slots(db_session, doctor, day.date(), zone=UTC_ZONE)

    # 09:00 to 17:00 in 30-minute appointments.
    assert len(slots) == 16
    assert slots[0].starts_at == slot_at(day, 9, 0)
    assert slots[0].ends_at == slot_at(day, 9, 30)
    assert slots[-1].starts_at == slot_at(day, 16, 30)


async def test_a_leave_day_offers_nothing(
    db_session: AsyncSession, bookable_doctor: DoctorProfile
) -> None:
    day = a_future_day()
    db_session.add(DoctorLeaveDay(doctor_profile_id=bookable_doctor.id, leave_date=day.date()))
    await db_session.commit()
    doctor = await _reload(db_session, bookable_doctor)

    slots = await available_slots(db_session, doctor, day.date(), zone=UTC_ZONE)

    assert slots == []


async def test_a_day_the_doctor_does_not_work_offers_nothing(
    db_session: AsyncSession, make_doctor: Callable[..., Awaitable[DoctorProfile]]
) -> None:
    """This doctor has no working hours at all."""
    doctor_without_hours = await make_doctor()
    day = a_future_day()
    doctor = await _reload(db_session, doctor_without_hours)

    slots = await available_slots(db_session, doctor, day.date(), zone=UTC_ZONE)

    assert slots == []


async def test_a_confirmed_booking_removes_its_slot(
    db_session: AsyncSession, bookable_doctor: DoctorProfile, make_user: MakeUser
) -> None:
    day = a_future_day()
    taken = slot_at(day, 10, 0)
    patient = await make_user(role=UserRole.PATIENT)
    db_session.add(
        Appointment(
            patient_id=patient.id,
            doctor_profile_id=bookable_doctor.id,
            starts_at=taken,
            ends_at=taken + timedelta(minutes=30),
            status=AppointmentStatus.CONFIRMED,
        )
    )
    await db_session.commit()
    doctor = await _reload(db_session, bookable_doctor)

    slots = await available_slots(db_session, doctor, day.date(), zone=UTC_ZONE)

    assert taken not in [slot.starts_at for slot in slots]
    assert len(slots) == 15


async def test_a_live_hold_removes_its_slot(
    db_session: AsyncSession, bookable_doctor: DoctorProfile, make_user: MakeUser
) -> None:
    """Otherwise the symptom form would be a window for someone else to take the slot."""
    day = a_future_day()
    held = slot_at(day, 11, 0)
    patient = await make_user(role=UserRole.PATIENT)
    db_session.add(
        Appointment(
            patient_id=patient.id,
            doctor_profile_id=bookable_doctor.id,
            starts_at=held,
            ends_at=held + timedelta(minutes=30),
            status=AppointmentStatus.HELD,
            hold_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    await db_session.commit()
    doctor = await _reload(db_session, bookable_doctor)

    slots = await available_slots(db_session, doctor, day.date(), zone=UTC_ZONE)

    assert held not in [slot.starts_at for slot in slots]


async def test_an_expired_hold_releases_its_slot(
    db_session: AsyncSession, bookable_doctor: DoctorProfile, make_user: MakeUser
) -> None:
    """No sweeper job reclaims abandoned holds — they simply stop counting once they lapse."""
    day = a_future_day()
    abandoned = slot_at(day, 12, 0)
    patient = await make_user(role=UserRole.PATIENT)
    db_session.add(
        Appointment(
            patient_id=patient.id,
            doctor_profile_id=bookable_doctor.id,
            starts_at=abandoned,
            ends_at=abandoned + timedelta(minutes=30),
            status=AppointmentStatus.HELD,
            hold_expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await db_session.commit()
    doctor = await _reload(db_session, bookable_doctor)

    slots = await available_slots(db_session, doctor, day.date(), zone=UTC_ZONE)

    assert abandoned in [slot.starts_at for slot in slots]


async def test_a_cancelled_booking_releases_its_slot(
    db_session: AsyncSession, bookable_doctor: DoctorProfile, make_user: MakeUser
) -> None:
    day = a_future_day()
    freed = slot_at(day, 13, 0)
    patient = await make_user(role=UserRole.PATIENT)
    db_session.add(
        Appointment(
            patient_id=patient.id,
            doctor_profile_id=bookable_doctor.id,
            starts_at=freed,
            ends_at=freed + timedelta(minutes=30),
            status=AppointmentStatus.CANCELLED_BY_PATIENT,
            cancelled_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    doctor = await _reload(db_session, bookable_doctor)

    slots = await available_slots(db_session, doctor, day.date(), zone=UTC_ZONE)

    assert freed in [slot.starts_at for slot in slots]


async def test_slots_already_started_are_not_offered(
    db_session: AsyncSession, bookable_doctor: DoctorProfile
) -> None:
    day = a_future_day()
    doctor = await _reload(db_session, bookable_doctor)
    # Pretend it is already midday on that date.
    pretend_now = slot_at(day, 12, 0)

    slots = await available_slots(db_session, doctor, day.date(), zone=UTC_ZONE, now=pretend_now)

    assert all(slot.starts_at > pretend_now for slot in slots)
    assert slots[0].starts_at == slot_at(day, 12, 30)


async def test_a_deactivated_doctor_offers_nothing(
    db_session: AsyncSession, bookable_doctor: DoctorProfile
) -> None:
    doctor = await _reload(db_session, bookable_doctor)
    doctor.user.is_active = False
    await db_session.commit()

    slots = await available_slots(db_session, doctor, a_future_day().date(), zone=UTC_ZONE)

    assert slots == []


async def test_slots_are_read_in_the_clinic_timezone(
    db_session: AsyncSession, bookable_doctor: DoctorProfile
) -> None:
    """09:00 in the clinic's zone is a different UTC instant, and that is what gets stored."""
    day = a_future_day()
    doctor = await _reload(db_session, bookable_doctor)

    slots = await available_slots(db_session, doctor, day.date(), zone=ZoneInfo("Asia/Kolkata"))

    # 09:00 IST is 03:30 UTC.
    assert slots[0].starts_at == slot_at(day, 3, 30)
