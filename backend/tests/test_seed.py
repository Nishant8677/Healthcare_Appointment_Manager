"""The demo seed.

Two things worth testing here. The guards, because the failure they prevent — adding
shared-password logins to a live clinic database — is one you only make once. And the fact
that the seed produces every appointment state, because its whole purpose is that a reviewer
opening the deployment finds something on each screen rather than three empty lists.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.appointment import Appointment
from app.models.clinical import AiSummary, Prescription
from app.models.doctor import DoctorLeaveDay, DoctorProfile
from app.models.enums import AppointmentStatus, NotificationType, UserRole
from app.models.notification import NotificationJob
from app.models.user import User
from app.seed import ADMIN_EMAIL, DOCTORS, PATIENTS, SeedRefused, seed_demo

MakeUser = Callable[..., Awaitable[User]]
ModelT = TypeVar("ModelT")

DEMO_PASSWORD = "seed-test-password"


async def rows(session: AsyncSession, model: type[ModelT]) -> list[ModelT]:
    result = await session.execute(select(model))
    return list(result.scalars().all())


async def test_seeding_an_empty_database_creates_the_whole_clinic(
    db_session: AsyncSession, settings: Settings
) -> None:
    report = await seed_demo(db_session, settings=settings, password=DEMO_PASSWORD)

    assert report.created is True

    users = await rows(db_session, User)
    by_role = {role: [u for u in users if u.role is role] for role in UserRole}
    assert len(by_role[UserRole.ADMIN]) == 1
    assert len(by_role[UserRole.DOCTOR]) == len(DOCTORS)
    assert len(by_role[UserRole.PATIENT]) == len(PATIENTS)


async def test_doctors_have_hours_on_every_day(
    db_session: AsyncSession, settings: Settings
) -> None:
    """A reviewer landing on any date should see slots, not an empty day they navigate away
    from and never come back to."""
    await seed_demo(db_session, settings=settings, password=DEMO_PASSWORD)

    for doctor in await rows(db_session, DoctorProfile):
        await db_session.refresh(doctor, ["working_hours"])
        assert {window.weekday for window in doctor.working_hours} == set(range(7))


async def test_every_appointment_state_is_represented(
    db_session: AsyncSession, settings: Settings
) -> None:
    await seed_demo(db_session, settings=settings, password=DEMO_PASSWORD)

    statuses = {appointment.status for appointment in await rows(db_session, Appointment)}
    assert AppointmentStatus.CONFIRMED in statuses, "an upcoming booking to look at"
    assert AppointmentStatus.COMPLETED in statuses, "a finished visit with a summary"
    assert AppointmentStatus.CANCELLED_BY_CLINIC in statuses, "one the leave cascade cancelled"


async def test_the_completed_visit_carries_a_prescription_and_its_reminders(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Seeded through `record_visit`, so the reminders are the real ones: 2 a day for 7 days."""
    await seed_demo(db_session, settings=settings, password=DEMO_PASSWORD)

    assert len(await rows(db_session, Prescription)) == 1

    reminders = [
        job
        for job in await rows(db_session, NotificationJob)
        if job.notification_type is NotificationType.MEDICATION_REMINDER
    ]
    assert len(reminders) == 14


async def test_the_leave_cascade_actually_ran(db_session: AsyncSession, settings: Settings) -> None:
    """The cancelled appointment comes from `record_leave`, not from a status written by hand
    — so the leave day and the patient's notice exist too."""
    await seed_demo(db_session, settings=settings, password=DEMO_PASSWORD)

    assert len(await rows(db_session, DoctorLeaveDay)) == 1
    notices = [
        job
        for job in await rows(db_session, NotificationJob)
        if job.notification_type is NotificationType.LEAVE_CONFLICT
    ]
    assert len(notices) == 1


async def test_summaries_are_requested_for_the_seeded_visits(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Booking and recording a visit each queue a summary, so the AI panels have something to
    show once the worker runs."""
    await seed_demo(db_session, settings=settings, password=DEMO_PASSWORD)

    summaries = await rows(db_session, AiSummary)
    assert len(summaries) >= 2


async def test_running_twice_changes_nothing(db_session: AsyncSession, settings: Settings) -> None:
    """Deploy scripts get re-run. A second pass must not double the demo clinic."""
    await seed_demo(db_session, settings=settings, password=DEMO_PASSWORD)
    before = len(await rows(db_session, User))

    second = await seed_demo(db_session, settings=settings, password=DEMO_PASSWORD)

    assert second.created is False
    assert len(await rows(db_session, User)) == before


async def test_it_refuses_a_database_that_already_has_accounts(
    db_session: AsyncSession, settings: Settings, make_user: MakeUser
) -> None:
    """The guard that matters: an empty database is by definition not a running clinic, one
    with accounts in it might be."""
    await make_user(role=UserRole.ADMIN, email="ops@realclinic.example.com")

    with pytest.raises(SeedRefused, match="may be a real deployment"):
        await seed_demo(db_session, settings=settings, password=DEMO_PASSWORD)


async def test_the_refusal_leaves_the_database_untouched(
    db_session: AsyncSession, settings: Settings, make_user: MakeUser
) -> None:
    await make_user(role=UserRole.ADMIN, email="ops@realclinic.example.com")

    with pytest.raises(SeedRefused):
        await seed_demo(db_session, settings=settings, password=DEMO_PASSWORD)

    users = await rows(db_session, User)
    assert len(users) == 1
    assert all(user.email != ADMIN_EMAIL for user in users)


async def test_demo_accounts_can_actually_sign_in(
    db_session: AsyncSession, settings: Settings
) -> None:
    """The passwords are hashed by the same code the login path verifies with — worth
    checking, because a seed whose accounts cannot sign in is worse than no seed."""
    from app.services.auth_service import authenticate

    await seed_demo(db_session, settings=settings, password=DEMO_PASSWORD)

    admin = await authenticate(db_session, email=ADMIN_EMAIL, password=DEMO_PASSWORD)
    assert admin.role is UserRole.ADMIN

    doctor = await authenticate(db_session, email=DOCTORS[0].email, password=DEMO_PASSWORD)
    assert doctor.role is UserRole.DOCTOR

    patient = await authenticate(db_session, email=PATIENTS[0].email, password=DEMO_PASSWORD)
    assert patient.role is UserRole.PATIENT
