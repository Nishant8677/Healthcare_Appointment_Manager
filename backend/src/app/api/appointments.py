"""Booking endpoints: hold, confirm, cancel, reschedule and list.

Each domain error maps to one status code here, and only here, so a rule cannot end up
meaning two different things at two different call sites:

* `404` — no such appointment, *or* it belongs to someone else (deliberately identical).
* `409` — the request was reasonable but the world disagrees: slot gone, already holding,
  already cancelled.
* `410` — the hold existed and has lapsed. Distinct from 409 because the client's correct
  next move is different: start again rather than retry.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_app_settings, get_current_user, get_session, require_roles
from app.core.config import Settings
from app.core.exceptions import (
    ActiveHoldExists,
    AppointmentNotCancellable,
    AppointmentNotConfirmable,
    AppointmentNotFound,
    DoctorNotFound,
    HoldExpired,
    SlotTaken,
    SlotUnavailable,
    VisitAlreadyRecorded,
    VisitNotCompletable,
)
from app.models.appointment import Appointment
from app.models.clinical import AiSummary
from app.models.enums import SummaryStatus, SummaryType, UserRole
from app.models.user import User
from app.schemas.appointment import (
    AppointmentResponse,
    CancelRequest,
    DoctorSummary,
    HoldRequest,
    PatientSummary,
    RescheduleRequest,
    SymptomFormRequest,
    SymptomReportResponse,
)
from app.schemas.visit import (
    RecordVisitRequest,
    RecordVisitResponse,
    SummaryResponse,
)
from app.services import booking_service, summaries, visit_service

router = APIRouter(prefix="/appointments", tags=["appointments"])

# Booking is a patient action; doctors and admins manage appointments through their own views.
patient_only = require_roles(UserRole.PATIENT)
# Recording clinical notes is the doctor's job; an admin may do it for them.
clinical_staff = require_roles(UserRole.DOCTOR, UserRole.ADMIN)


def _to_response(appointment: Appointment) -> AppointmentResponse:
    report = appointment.symptom_report
    return AppointmentResponse(
        id=appointment.id,
        status=appointment.status,
        starts_at=appointment.starts_at,
        ends_at=appointment.ends_at,
        doctor=DoctorSummary(
            id=appointment.doctor_profile.id,
            full_name=appointment.doctor_profile.user.full_name,
            specialisation=appointment.doctor_profile.specialisation,
            slot_duration_minutes=appointment.doctor_profile.slot_duration_minutes,
        ),
        patient=PatientSummary(id=appointment.patient.id, full_name=appointment.patient.full_name),
        hold_expires_at=appointment.hold_expires_at,
        cancellation_reason=appointment.cancellation_reason,
        symptom_report=(
            None
            if report is None
            else SymptomReportResponse(
                symptoms=report.symptoms,
                duration_days=report.duration_days,
                additional_notes=report.additional_notes,
            )
        ),
    )


def _not_found(appointment_id: uuid.UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"No appointment with id {appointment_id}.",
    )


def _conflict(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)


@router.post(
    "/hold",
    response_model=AppointmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Hold a slot while completing the symptom form",
)
async def hold_slot(
    payload: HoldRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
    patient: User = Depends(patient_only),
) -> AppointmentResponse:
    """Reserve a slot for a few minutes.

    Without this step the slot could be taken while the patient is still typing their
    symptoms, which is both a poor experience and a race the confirm step would have to lose.
    """
    try:
        appointment = await booking_service.hold_slot(
            session,
            patient=patient,
            doctor_id=payload.doctor_id,
            starts_at=payload.starts_at,
            settings=settings,
        )
    except DoctorNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No doctor with id {payload.doctor_id}.",
        ) from None
    except ActiveHoldExists as error:
        raise _conflict(
            "You already have a slot on hold. Confirm or cancel it before holding another "
            f"(appointment {error})."
        ) from None
    except (SlotUnavailable, SlotTaken) as error:
        raise _conflict(str(error)) from None

    return _to_response(appointment)


@router.post(
    "/{appointment_id}/confirm",
    response_model=AppointmentResponse,
    summary="Confirm a held slot with the symptom form",
)
async def confirm_appointment(
    appointment_id: uuid.UUID,
    payload: SymptomFormRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
    patient: User = Depends(patient_only),
) -> AppointmentResponse:
    try:
        appointment = await booking_service.confirm_hold(
            session,
            appointment_id=appointment_id,
            patient=patient,
            settings=settings,
            symptoms=payload.symptoms,
            duration_days=payload.duration_days,
            additional_notes=payload.additional_notes,
        )
    except AppointmentNotFound:
        raise _not_found(appointment_id) from None
    except HoldExpired as error:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(error)) from None
    except AppointmentNotConfirmable as error:
        raise _conflict(str(error)) from None

    return _to_response(appointment)


@router.post(
    "/{appointment_id}/cancel",
    response_model=AppointmentResponse,
    summary="Cancel an appointment",
)
async def cancel_appointment(
    appointment_id: uuid.UUID,
    payload: CancelRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
    actor: User = Depends(get_current_user),
) -> AppointmentResponse:
    """Patients cancel their own; doctors cancel their own schedule; admins cancel any.

    The resulting status records *who* cancelled, because the clinic cancelling and the
    patient cancelling warrant different messages in Phase 5.
    """
    try:
        appointment = await booking_service.cancel_appointment(
            session,
            appointment_id=appointment_id,
            actor=actor,
            settings=settings,
            reason=payload.reason,
        )
    except AppointmentNotFound:
        raise _not_found(appointment_id) from None
    except AppointmentNotCancellable as error:
        raise _conflict(str(error)) from None

    return _to_response(appointment)


@router.post(
    "/{appointment_id}/reschedule",
    response_model=AppointmentResponse,
    summary="Move an appointment to another slot",
)
async def reschedule_appointment(
    appointment_id: uuid.UUID,
    payload: RescheduleRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
    patient: User = Depends(patient_only),
) -> AppointmentResponse:
    """Returns the *new* appointment. The original is cancelled in the same transaction."""
    try:
        appointment = await booking_service.reschedule_appointment(
            session,
            appointment_id=appointment_id,
            patient=patient,
            new_starts_at=payload.starts_at,
            settings=settings,
        )
    except AppointmentNotFound:
        raise _not_found(appointment_id) from None
    except AppointmentNotConfirmable as error:
        raise _conflict(str(error)) from None
    except (SlotUnavailable, SlotTaken) as error:
        raise _conflict(str(error)) from None

    return _to_response(appointment)


@router.get("", response_model=list[AppointmentResponse], summary="My appointments")
async def list_appointments(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    include_cancelled: bool = Query(default=False),
) -> list[AppointmentResponse]:
    """Scoped by role: a patient's own bookings, a doctor's own schedule, everything for an
    admin. The scoping is in the query, not a filter applied afterwards."""
    appointments = await booking_service.list_appointments(
        session, user=user, include_cancelled=include_cancelled
    )
    return [_to_response(appointment) for appointment in appointments]


def _summary_response(summary: AiSummary) -> SummaryResponse:
    """Report the summary honestly, including when it is not there.

    A pending or failed summary is returned as such rather than as `null`, because "being
    prepared" and "could not be produced" mean different things to a doctor deciding whether
    to wait.
    """
    reason: str | None = None
    if summary.status is SummaryStatus.PENDING:
        reason = "The summary is still being prepared."
    elif summary.status is SummaryStatus.FAILED:
        reason = "The summary could not be generated. The clinical record is unaffected."

    return SummaryResponse(
        summary_type=summary.summary_type,
        status=summary.status,
        urgency=summary.urgency,
        content=summary.content,
        prompt_version=summary.prompt_version,
        model=summary.model,
        attempts=summary.attempts,
        unavailable_reason=reason,
    )


@router.post(
    "/{appointment_id}/visit",
    response_model=RecordVisitResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record clinical notes and a prescription",
)
async def record_visit(
    appointment_id: uuid.UUID,
    payload: RecordVisitRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
    doctor: User = Depends(clinical_staff),
) -> RecordVisitResponse:
    """Close out a visit.

    The patient-friendly summary is *requested* here, not generated: the doctor is not made
    to wait on a language model before moving to the next patient. Medication reminders are
    scheduled from the prescription's structured fields.
    """
    try:
        outcome = await visit_service.record_visit(
            session,
            appointment_id=appointment_id,
            doctor=doctor,
            clinical_notes=payload.clinical_notes,
            settings=settings,
            medications=[
                visit_service.MedicationInput(
                    drug_name=medication.drug_name,
                    dosage=medication.dosage,
                    times_per_day=medication.times_per_day,
                    duration_days=medication.duration_days,
                    instructions=medication.instructions,
                )
                for medication in payload.medications
            ],
            follow_up_date=payload.follow_up_date,
        )
    except AppointmentNotFound:
        raise _not_found(appointment_id) from None
    except VisitAlreadyRecorded:
        raise _conflict("Clinical notes have already been recorded for this appointment.") from None
    except VisitNotCompletable as error:
        raise _conflict(str(error)) from None

    return RecordVisitResponse(
        appointment_id=outcome.appointment.id,
        status=outcome.appointment.status.value,
        completed_at=outcome.appointment.updated_at,
        reminders_scheduled=outcome.reminders_scheduled,
    )


@router.get(
    "/{appointment_id}/pre-visit-summary",
    response_model=SummaryResponse,
    summary="The doctor's brief for an appointment",
)
async def pre_visit_summary(
    appointment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(clinical_staff),
) -> SummaryResponse:
    """Clinical staff only. A patient must not see a triage judgement about themselves that
    no clinician has reviewed."""
    appointment = await _visible_appointment(session, appointment_id, user)
    summary = await summaries.summary_for(session, appointment.id, SummaryType.PRE_VISIT)
    if summary is None:
        raise _not_found(appointment_id)
    return _summary_response(summary)


@router.get(
    "/{appointment_id}/post-visit-summary",
    response_model=SummaryResponse,
    summary="The patient's plain-language summary",
)
async def post_visit_summary(
    appointment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> SummaryResponse:
    appointment = await _visible_appointment(session, appointment_id, user)
    summary = await summaries.summary_for(session, appointment.id, SummaryType.POST_VISIT)
    if summary is None:
        raise _not_found(appointment_id)
    return _summary_response(summary)


async def _visible_appointment(
    session: AsyncSession, appointment_id: uuid.UUID, user: User
) -> Appointment:
    """Load an appointment this user is entitled to see, or report it missing."""
    try:
        appointment = await booking_service.load_appointment(session, appointment_id)
    except AppointmentNotFound:
        raise _not_found(appointment_id) from None

    if user.role is UserRole.PATIENT and appointment.patient_id != user.id:
        raise _not_found(appointment_id)
    if user.role is UserRole.DOCTOR and appointment.doctor_profile.user_id != user.id:
        raise _not_found(appointment_id)
    return appointment
