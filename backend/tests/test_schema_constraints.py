"""Database-level guarantees.

These assert on the schema itself rather than on service code. The partial unique index in
particular is the last line of defence against double booking — the layer that still holds
if the Phase 3 booking logic is wrong — so it is verified directly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Appointment, AppointmentStatus, DoctorProfile, User, UserRole
from app.models.appointment import _OCCUPYING_SQL
from app.models.enums import OCCUPYING_STATUSES

MakeUser = Callable[..., Awaitable[User]]
MakeDoctor = Callable[..., Awaitable[DoctorProfile]]

SLOT_START = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def _appointment(
    patient: User,
    doctor: DoctorProfile,
    *,
    status: AppointmentStatus,
    starts_at: datetime = SLOT_START,
) -> Appointment:
    return Appointment(
        patient_id=patient.id,
        doctor_profile_id=doctor.id,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(minutes=30),
        status=status,
        # Required by the `held_appointment_has_expiry` check constraint.
        hold_expires_at=(
            starts_at + timedelta(minutes=5) if status is AppointmentStatus.HELD else None
        ),
    )


def test_index_predicate_matches_the_occupying_status_list() -> None:
    """The SQL literal in the index and the Python tuple must not drift apart — a mismatch
    would silently narrow which bookings the database protects."""
    literal = {part.strip().strip("'") for part in _OCCUPYING_SQL.strip("()").split(",")}

    assert literal == {status.value for status in OCCUPYING_STATUSES}


async def test_two_confirmed_appointments_cannot_share_a_slot(
    db_session: AsyncSession, make_user: MakeUser, make_doctor: MakeDoctor
) -> None:
    doctor = await make_doctor()
    first_patient = await make_user(role=UserRole.PATIENT)
    second_patient = await make_user(role=UserRole.PATIENT)

    db_session.add(_appointment(first_patient, doctor, status=AppointmentStatus.CONFIRMED))
    await db_session.commit()

    db_session.add(_appointment(second_patient, doctor, status=AppointmentStatus.CONFIRMED))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_a_held_slot_also_blocks_a_second_booking(
    db_session: AsyncSession, make_user: MakeUser, make_doctor: MakeDoctor
) -> None:
    """A hold must reserve the slot, otherwise the symptom form is a race window."""
    doctor = await make_doctor()
    first_patient = await make_user(role=UserRole.PATIENT)
    second_patient = await make_user(role=UserRole.PATIENT)

    db_session.add(_appointment(first_patient, doctor, status=AppointmentStatus.HELD))
    await db_session.commit()

    db_session.add(_appointment(second_patient, doctor, status=AppointmentStatus.CONFIRMED))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_a_cancelled_appointment_frees_the_slot(
    db_session: AsyncSession, make_user: MakeUser, make_doctor: MakeDoctor
) -> None:
    """The index is partial precisely so cancelled bookings do not block rebooking."""
    doctor = await make_doctor()
    first_patient = await make_user(role=UserRole.PATIENT)
    second_patient = await make_user(role=UserRole.PATIENT)

    cancelled = _appointment(first_patient, doctor, status=AppointmentStatus.CANCELLED_BY_CLINIC)
    db_session.add(cancelled)
    await db_session.commit()

    db_session.add(_appointment(second_patient, doctor, status=AppointmentStatus.CONFIRMED))
    await db_session.commit()  # must not raise


async def test_the_same_slot_is_free_for_a_different_doctor(
    db_session: AsyncSession, make_user: MakeUser, make_doctor: MakeDoctor
) -> None:
    first_doctor = await make_doctor(specialisation="Cardiology")
    second_doctor = await make_doctor(specialisation="Dermatology")
    patient = await make_user(role=UserRole.PATIENT)

    db_session.add(_appointment(patient, first_doctor, status=AppointmentStatus.CONFIRMED))
    db_session.add(_appointment(patient, second_doctor, status=AppointmentStatus.CONFIRMED))

    await db_session.commit()  # must not raise


async def test_an_appointment_cannot_end_before_it_starts(
    db_session: AsyncSession, make_user: MakeUser, make_doctor: MakeDoctor
) -> None:
    doctor = await make_doctor()
    patient = await make_user(role=UserRole.PATIENT)

    db_session.add(
        Appointment(
            patient_id=patient.id,
            doctor_profile_id=doctor.id,
            starts_at=SLOT_START,
            ends_at=SLOT_START - timedelta(minutes=30),
            status=AppointmentStatus.CONFIRMED,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_a_held_appointment_must_carry_an_expiry(
    db_session: AsyncSession, make_user: MakeUser, make_doctor: MakeDoctor
) -> None:
    """A hold with no expiry would reserve a slot forever."""
    doctor = await make_doctor()
    patient = await make_user(role=UserRole.PATIENT)

    db_session.add(
        Appointment(
            patient_id=patient.id,
            doctor_profile_id=doctor.id,
            starts_at=SLOT_START,
            ends_at=SLOT_START + timedelta(minutes=30),
            status=AppointmentStatus.HELD,
            hold_expires_at=None,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_a_doctor_cannot_have_two_profiles(
    db_session: AsyncSession, make_doctor: MakeDoctor
) -> None:
    doctor = await make_doctor()

    db_session.add(
        DoctorProfile(
            user_id=doctor.user_id,
            specialisation="Second Speciality",
            slot_duration_minutes=20,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.parametrize("bad_duration", [0, -15, 400])
async def test_slot_duration_must_be_sensible(
    db_session: AsyncSession, make_user: MakeUser, bad_duration: int
) -> None:
    doctor_user = await make_user(role=UserRole.DOCTOR)

    db_session.add(
        DoctorProfile(
            user_id=doctor_user.id,
            specialisation="Cardiology",
            slot_duration_minutes=bad_duration,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.commit()
