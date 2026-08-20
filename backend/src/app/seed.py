"""Demo data for a fresh deployment.

Builds a clinic that already looks like it has been running: doctors with different
specialisations and hours, patients, and appointments in every state the system has — an
upcoming booking with a symptom form, a completed visit with a prescription and its
reminders, and one appointment the clinic cancelled because the doctor took leave.

**Everything goes through the real services**, not through direct inserts. A booking made by
`booking_service` writes its notification outbox rows, requests its AI summary and queues its
calendar sync exactly as a patient's would, so what a reviewer opens is the system's own
output rather than a tableau arranged to look like it. It also means this script fails if the
booking rules are broken, which is a small extra test for free.

Passwords come from `DEMO_PASSWORD`; there are none in this file. A public repository should
not contain a working credential for a hosted deployment, even a deliberately disposable one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import hash_password
from app.models.appointment import Appointment
from app.models.doctor import DoctorProfile
from app.models.enums import UserRole
from app.models.user import User
from app.services import auth_service, booking_service, doctor_service, leave_service
from app.services.availability import available_slots
from app.services.scheduling import WorkingWindow
from app.services.visit_service import MedicationInput, record_visit

logger = logging.getLogger(__name__)

ADMIN_EMAIL = "admin@clinic.demo"
WEEKDAYS = tuple(range(7))


@dataclass(frozen=True, slots=True)
class DoctorSpec:
    email: str
    full_name: str
    specialisation: str
    slot_duration_minutes: int
    start: time
    end: time


# Every day of the week, so a reviewer opening the booking screen on any date sees free slots
# rather than an empty day they have to navigate away from.
DOCTORS: tuple[DoctorSpec, ...] = (
    DoctorSpec(
        email="asha.rao@clinic.demo",
        full_name="Asha Rao",
        specialisation="Cardiology",
        slot_duration_minutes=30,
        start=time(9, 0),
        end=time(17, 0),
    ),
    DoctorSpec(
        email="nikhil.bose@clinic.demo",
        full_name="Nikhil Bose",
        specialisation="Dermatology",
        slot_duration_minutes=20,
        start=time(10, 0),
        end=time(16, 0),
    ),
    DoctorSpec(
        email="fatima.khan@clinic.demo",
        full_name="Fatima Khan",
        specialisation="General Medicine",
        slot_duration_minutes=15,
        start=time(8, 30),
        end=time(13, 30),
    ),
)


@dataclass(frozen=True, slots=True)
class PatientSpec:
    email: str
    full_name: str


PATIENTS: tuple[PatientSpec, ...] = (
    PatientSpec(email="meera.iyer@example.com", full_name="Meera Iyer"),
    PatientSpec(email="daniel.osei@example.com", full_name="Daniel Osei"),
    PatientSpec(email="priya.nair@example.com", full_name="Priya Nair"),
)


@dataclass(frozen=True, slots=True)
class SeedReport:
    """What the seed did. `created` is false when the database was already populated."""

    created: bool
    lines: list[str]


class SeedRefused(Exception):
    """The database is not one this script is willing to write demo data into."""


async def _count_users(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(User))
    return int(result.scalar_one())


async def _find_user(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def seed_demo(session: AsyncSession, *, settings: Settings, password: str) -> SeedReport:
    """Populate a fresh database. Returns lines describing what was created.

    Raises:
        SeedRefused: the database already holds accounts that this script did not create.
    """
    existing_admin = await _find_user(session, ADMIN_EMAIL)
    if existing_admin is not None:
        return SeedReport(
            created=False,
            # Plain ASCII: this is printed to a console whose encoding is not ours to choose,
            # and a mangled character in the first line a deployer sees looks like a fault.
            lines=["Already seeded - the demo admin exists. Nothing was changed."],
        )

    # The guard that matters. An empty database is by definition not a running clinic; one
    # with accounts in it might be, and adding shared-password logins to a real medical
    # system would be a serious thing to do by accident.
    if await _count_users(session) > 0:
        raise SeedRefused(
            "This database already contains accounts that were not created by the demo seed. "
            "Refusing to add demo logins to what may be a real deployment."
        )

    lines: list[str] = []

    admin = User(
        email=ADMIN_EMAIL,
        password_hash=hash_password(password),
        full_name="Clinic Administrator",
        role=UserRole.ADMIN,
    )
    session.add(admin)
    await session.commit()
    lines.append(f"admin    {ADMIN_EMAIL}")

    doctors = []
    for doctor_spec in DOCTORS:
        profile = await doctor_service.create_doctor(
            session,
            email=doctor_spec.email,
            password=password,
            full_name=doctor_spec.full_name,
            specialisation=doctor_spec.specialisation,
            slot_duration_minutes=doctor_spec.slot_duration_minutes,
            working_hours=[
                WorkingWindow(weekday=weekday, start=doctor_spec.start, end=doctor_spec.end)
                for weekday in WEEKDAYS
            ],
        )
        doctors.append(profile)
        lines.append(f"doctor   {doctor_spec.email}  ({doctor_spec.specialisation})")

    patients = []
    for patient_spec in PATIENTS:
        patient = await auth_service.register_patient(
            session,
            email=patient_spec.email,
            password=password,
            full_name=patient_spec.full_name,
        )
        patients.append(patient)
        lines.append(f"patient  {patient_spec.email}")

    now = datetime.now(UTC)
    lines.extend(await _seed_appointments(session, settings, doctors, patients, now))
    return SeedReport(created=True, lines=lines)


async def _first_free_slot(
    session: AsyncSession,
    doctor: DoctorProfile,
    day: date,
    *,
    settings: Settings,
    now: datetime,
) -> datetime | None:
    slots = await available_slots(session, doctor, day, zone=settings.clinic_zone, now=now)
    return slots[0].starts_at if slots else None


async def _book(
    session: AsyncSession,
    *,
    settings: Settings,
    patient: User,
    doctor: DoctorProfile,
    day: date,
    symptoms: str,
    duration_days: int,
    now: datetime,
) -> Appointment | None:
    """Hold a slot and confirm it, exactly as the booking endpoints do."""
    starts_at = await _first_free_slot(session, doctor, day, settings=settings, now=now)
    if starts_at is None:
        return None

    held = await booking_service.hold_slot(
        session,
        patient=patient,
        doctor_id=doctor.id,
        starts_at=starts_at,
        settings=settings,
        now=now,
    )
    return await booking_service.confirm_hold(
        session,
        appointment_id=held.id,
        patient=patient,
        symptoms=symptoms,
        settings=settings,
        duration_days=duration_days,
        now=now,
    )


async def _seed_appointments(
    session: AsyncSession,
    settings: Settings,
    doctors: list[DoctorProfile],
    patients: list[User],
    now: datetime,
) -> list[str]:
    """One appointment in each state the portals need to show."""
    lines: list[str] = []
    today = now.astimezone(settings.clinic_zone).date()

    # 1. Upcoming and confirmed. The doctor's pre-visit brief is generated for this one, so
    #    the triage screen has something on it as soon as the worker runs.
    upcoming = await _book(
        session,
        settings=settings,
        patient=patients[0],
        doctor=doctors[0],
        day=today + timedelta(days=2),
        symptoms=(
            "Tightness across my chest when I climb stairs, and I get out of breath much "
            "faster than I used to. It settles after a few minutes of sitting down."
        ),
        duration_days=12,
        now=now,
    )
    if upcoming is not None:
        lines.append("appointment  upcoming, confirmed, with a symptom form")

    # 2. A visit that has happened, with a prescription — which is what puts a post-visit
    #    summary and a run of medication reminders in front of the patient.
    past_day = today - timedelta(days=3)
    completed = await _book(
        session,
        settings=settings,
        patient=patients[1],
        doctor=doctors[1],
        day=past_day,
        symptoms=(
            "An itchy red rash on both forearms that flares up after I use soap. It has "
            "spread over the last fortnight."
        ),
        duration_days=14,
        # Booked before the appointment, otherwise the slot is in the past and unbookable.
        now=now - timedelta(days=5),
    )
    if completed is not None:
        doctor_user = await session.get(User, doctors[1].user_id)
        if doctor_user is not None:
            visit = await record_visit(
                session,
                appointment_id=completed.id,
                doctor=doctor_user,
                clinical_notes=(
                    "Contact dermatitis on both forearms, consistent with an irritant "
                    "reaction to a fragranced soap. No secondary infection. Advised a "
                    "fragrance-free wash and a short course of topical steroid."
                ),
                settings=settings,
                medications=[
                    MedicationInput(
                        drug_name="Hydrocortisone 1% cream",
                        dosage="Thin layer",
                        times_per_day=2,
                        duration_days=7,
                        instructions="Apply to the affected skin only.",
                    )
                ],
                follow_up_date=today + timedelta(days=14),
                now=now,
            )
            lines.append(
                f"appointment  completed, with a prescription "
                f"({visit.reminders_scheduled} medication reminders queued)"
            )

    # 3. A day the clinic cancelled. Books first, then takes the doctor off — so the
    #    cancellation and its notification are produced by the real leave cascade.
    leave_day = today + timedelta(days=5)
    cancelled = await _book(
        session,
        settings=settings,
        patient=patients[2],
        doctor=doctors[2],
        day=leave_day,
        symptoms="A sore throat and a mild fever for the last four days, worse in the evenings.",
        duration_days=4,
        now=now,
    )
    if cancelled is not None:
        leave = await leave_service.record_leave(
            session,
            doctors[2].id,
            leave_date=leave_day,
            settings=settings,
            reason="Conference",
            cancel_existing_appointments=True,
            now=now,
        )
        lines.append(
            f"leave day    {leave_day.isoformat()} "
            f"({leave.cancelled} appointment cancelled, "
            f"{leave.patients_notified} patient notified)"
        )

    return lines
