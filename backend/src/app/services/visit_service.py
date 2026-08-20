"""Completing a visit: clinical notes, prescription, summary and medication reminders."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppointmentNotFound, VisitAlreadyRecorded, VisitNotCompletable
from app.models.appointment import Appointment
from app.models.clinical import Prescription, PrescriptionMedication
from app.models.doctor import DoctorProfile
from app.models.enums import AppointmentStatus, SummaryType, UserRole
from app.models.user import User
from app.services import notifications, summaries
from app.services.booking_service import load_appointment

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MedicationInput:
    drug_name: str
    dosage: str
    times_per_day: int
    duration_days: int
    instructions: str | None = None


@dataclass(frozen=True, slots=True)
class VisitOutcome:
    appointment: Appointment
    reminders_scheduled: int


async def record_visit(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    doctor: User,
    clinical_notes: str,
    settings: Settings,
    medications: Sequence[MedicationInput] = (),
    follow_up_date: date | None = None,
    now: datetime | None = None,
) -> VisitOutcome:
    """Close out an appointment.

    Everything lands in one transaction: the prescription, the completed status, the pending
    post-visit summary, and every medication reminder. The summary is only *requested* here —
    generating it happens in the background, so a slow or unavailable model cannot stop a
    doctor finishing their notes and moving to the next patient.

    Raises:
        AppointmentNotFound, VisitNotCompletable, VisitAlreadyRecorded.
    """
    reference = now or datetime.now(UTC)
    appointment = await load_appointment(session, appointment_id)

    own_profile = await session.execute(
        select(DoctorProfile.id).where(DoctorProfile.user_id == doctor.id)
    )
    profile_id = own_profile.scalar_one_or_none()

    # An appointment belonging to another doctor is reported as missing rather than
    # forbidden, so the response cannot be used to probe for appointment ids.
    if doctor.role is UserRole.DOCTOR and (
        profile_id is None or appointment.doctor_profile_id != profile_id
    ):
        raise AppointmentNotFound(str(appointment_id))

    if appointment.status is AppointmentStatus.COMPLETED:
        raise VisitAlreadyRecorded(str(appointment_id))
    if appointment.status is not AppointmentStatus.CONFIRMED:
        raise VisitNotCompletable(
            f"Only a confirmed appointment can be completed; this one is "
            f"{appointment.status.value}."
        )

    prescription = Prescription(
        appointment_id=appointment.id,
        clinical_notes=clinical_notes,
        follow_up_date=follow_up_date,
        issued_at=reference,
        medications=[
            PrescriptionMedication(
                drug_name=medication.drug_name,
                dosage=medication.dosage,
                times_per_day=medication.times_per_day,
                duration_days=medication.duration_days,
                instructions=medication.instructions,
            )
            for medication in medications
        ],
    )
    session.add(prescription)
    appointment.status = AppointmentStatus.COMPLETED

    # Requested, not generated: the model is not on this request's critical path.
    summaries.queue_summary(
        session,
        appointment_id=appointment.id,
        summary_type=SummaryType.POST_VISIT,
        model=settings.llm_model,
    )

    # Any reminder for the appointment itself is now moot.
    await notifications.drop_pending_reminders(session, appointment.id)

    reminders = notifications.enqueue_medication_reminders(
        session,
        appointment=appointment,
        patient=appointment.patient,
        prescription=prescription,
        zone=settings.clinic_zone,
        max_days=settings.medication_reminder_max_days,
        first_hour=settings.medication_first_dose_hour,
        last_hour=settings.medication_last_dose_hour,
        now=reference,
    )

    await session.commit()
    logger.info(
        "visit recorded",
        extra={"appointment_id": str(appointment.id), "reminders_scheduled": reminders},
    )
    return VisitOutcome(
        appointment=await load_appointment(session, appointment.id),
        reminders_scheduled=reminders,
    )
