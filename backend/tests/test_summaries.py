"""AI summaries: prompts, generation, and — most importantly — what happens when the model
misbehaves.

The clinically significant property is that a slow, offline or refusing model degrades the
summary and nothing else. A booking must still complete; a doctor must still be able to file
their notes.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import DoctorProfile, User
from app.models.clinical import AiSummary
from app.models.enums import SummaryStatus, SummaryType, UrgencyLevel
from app.services import summaries
from app.services.llm import (
    LLMError,
    LLMRefusal,
    PostVisitSummary,
    PreVisitSummary,
    StubLLMClient,
)

Headers = dict[str, str]
MakeUser = Callable[..., Awaitable[User]]

SYMPTOMS = "Sore throat and mild fever for the last four days, worse in the evenings."
NOTES = "Viral pharyngitis. Rest, fluids, paracetamol as needed. Review if fever persists."


def a_future_day() -> datetime:
    return datetime.now(UTC) + timedelta(days=2)


def slot_at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day.date(), time(hour, minute), tzinfo=UTC)


class ScriptedLLM:
    """A client that answers, fails, or refuses on command."""

    def __init__(
        self, *, fail_times: int = 0, fail_forever: bool = False, refuse: bool = False
    ) -> None:
        self.calls = 0
        self.prompts: list[tuple[str, str]] = []
        self._fail_times = fail_times
        self._fail_forever = fail_forever
        self._refuse = refuse

    async def generate(
        self, *, system: str, user: str, output_model: type[Any], max_tokens: int
    ) -> Any:
        self.calls += 1
        self.prompts.append((system, user))
        if self._refuse:
            raise LLMRefusal("declined")
        if self._fail_forever or self.calls <= self._fail_times:
            raise LLMError("model unavailable")
        return await StubLLMClient().generate(
            system=system, user=user, output_model=output_model, max_tokens=max_tokens
        )


async def book(
    client: AsyncClient, headers: Headers, doctor: DoctorProfile, starts_at: datetime
) -> dict[str, Any]:
    held = await client.post(
        "/appointments/hold",
        headers=headers,
        json={"doctor_id": str(doctor.id), "starts_at": starts_at.isoformat()},
    )
    assert held.status_code == 201, held.text
    confirmed = await client.post(
        f"/appointments/{held.json()['id']}/confirm",
        headers=headers,
        json={"symptoms": SYMPTOMS, "duration_days": 4},
    )
    assert confirmed.status_code == 200, confirmed.text
    body: dict[str, Any] = confirmed.json()
    return body


async def summary_row(
    session: AsyncSession, appointment_id: str, summary_type: SummaryType
) -> AiSummary:
    session.expire_all()
    result = await session.execute(
        select(AiSummary).where(
            AiSummary.appointment_id == uuid.UUID(appointment_id),
            AiSummary.summary_type == summary_type,
        )
    )
    return result.scalar_one()


async def run_worker(
    session: AsyncSession, client: Any, settings: Settings, **kwargs: Any
) -> dict[str, int]:
    return await summaries.generate_due_summaries(
        session,
        client,
        model=settings.llm_model,
        max_output_tokens=settings.llm_max_output_tokens,
        **kwargs,
    )


# ---------------------------------------------------------------- queued, not generated


async def test_confirming_queues_a_pending_pre_visit_summary(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
) -> None:
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))

    summary = await summary_row(db_session, booked["id"], SummaryType.PRE_VISIT)

    assert summary.status is SummaryStatus.PENDING
    assert summary.content is None
    assert summary.prompt_version == summaries.PRE_VISIT_PROMPT_VERSION


async def test_booking_succeeds_even_though_no_summary_exists_yet(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    """The whole point: the model is not on the booking path."""
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))

    assert booked["status"] == "confirmed"


# ---------------------------------------------------------------- generation


async def test_the_worker_fills_in_a_pending_summary(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))

    report = await run_worker(db_session, StubLLMClient(), settings)

    assert report["ready"] == 1
    summary = await summary_row(db_session, booked["id"], SummaryType.PRE_VISIT)
    assert summary.status is SummaryStatus.READY
    assert summary.urgency is UrgencyLevel.MEDIUM
    assert summary.content is not None
    assert len(summary.content["suggested_questions"]) == 3


async def test_the_prompt_carries_the_patient_symptoms(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    scripted = ScriptedLLM()

    await run_worker(db_session, scripted, settings)

    system, user = scripted.prompts[0]
    assert SYMPTOMS in user
    assert "Reported duration: 4 day(s)" in user
    # The safety framing has to actually be sent, not just written down somewhere.
    assert "untrusted input" in system


async def test_a_second_pass_does_not_regenerate_a_ready_summary(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    """Regenerating would cost money and could produce different text for the same visit."""
    await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    scripted = ScriptedLLM()

    await run_worker(db_session, scripted, settings)
    second = await run_worker(db_session, scripted, settings)

    assert scripted.calls == 1
    assert second["ready"] == 0


# ---------------------------------------------------------------- degradation


async def test_a_failing_model_leaves_the_summary_pending_and_retries(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    moment = datetime.now(UTC)

    report = await run_worker(db_session, ScriptedLLM(fail_forever=True), settings, now=moment)

    assert report["retried"] == 1
    summary = await summary_row(db_session, booked["id"], SummaryType.PRE_VISIT)
    assert summary.status is SummaryStatus.PENDING
    assert summary.attempts == 1
    assert summary.next_attempt_at == moment + timedelta(minutes=2)
    assert "unavailable" in (summary.last_error or "")


async def test_a_summary_waiting_on_backoff_is_skipped(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    moment = datetime.now(UTC)
    await run_worker(db_session, ScriptedLLM(fail_forever=True), settings, now=moment)

    scripted = ScriptedLLM()
    too_early = await run_worker(db_session, scripted, settings, now=moment)
    on_time = await run_worker(db_session, scripted, settings, now=moment + timedelta(minutes=3))

    assert too_early["ready"] == 0
    assert on_time["ready"] == 1


async def test_a_transient_failure_eventually_succeeds(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    scripted = ScriptedLLM(fail_times=1)
    moment = datetime.now(UTC)

    await run_worker(db_session, scripted, settings, now=moment)
    await run_worker(db_session, scripted, settings, now=moment + timedelta(minutes=5))

    summary = await summary_row(db_session, booked["id"], SummaryType.PRE_VISIT)
    assert summary.status is SummaryStatus.READY
    assert summary.last_error is None


async def test_a_summary_is_parked_after_exhausting_its_retries(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    scripted = ScriptedLLM(fail_forever=True)
    moment = datetime.now(UTC)

    for _ in range(4):
        await run_worker(db_session, scripted, settings, now=moment, max_attempts=4)
        moment += timedelta(hours=2)

    summary = await summary_row(db_session, booked["id"], SummaryType.PRE_VISIT)
    assert summary.status is SummaryStatus.FAILED
    assert summary.attempts == 4


async def test_a_refusal_is_terminal_rather_than_retried(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    """The same request will be declined again; retrying only delays the same answer."""
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))

    report = await run_worker(db_session, ScriptedLLM(refuse=True), settings)

    assert report["failed"] == 1
    summary = await summary_row(db_session, booked["id"], SummaryType.PRE_VISIT)
    assert summary.status is SummaryStatus.FAILED
    assert summary.attempts == 1


async def test_a_failed_summary_is_reported_honestly_not_as_empty(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    settings: Settings,
    auth_header: Callable[[User], Headers],
) -> None:
    """A doctor must be able to tell "not ready" from "nothing to say"."""
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    await run_worker(db_session, ScriptedLLM(refuse=True), settings)
    doctor_user = await db_session.get(User, bookable_doctor.user_id)
    assert doctor_user is not None

    response = await client.get(
        f"/appointments/{booked['id']}/pre-visit-summary", headers=auth_header(doctor_user)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert "could not be generated" in body["unavailable_reason"]
    assert "clinical record is unaffected" in body["unavailable_reason"].lower()


async def test_a_pending_summary_says_so(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    db_session: AsyncSession,
    auth_header: Callable[[User], Headers],
) -> None:
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    doctor_user = await db_session.get(User, bookable_doctor.user_id)
    assert doctor_user is not None

    response = await client.get(
        f"/appointments/{booked['id']}/pre-visit-summary", headers=auth_header(doctor_user)
    )

    assert response.json()["status"] == "pending"
    assert "still being prepared" in response.json()["unavailable_reason"]


# ---------------------------------------------------------------- access control


async def test_a_patient_cannot_read_their_own_triage_brief(
    client: AsyncClient, patient_headers: Headers, bookable_doctor: DoctorProfile
) -> None:
    """An unreviewed urgency judgement about yourself is not something to read alone."""
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))

    response = await client.get(
        f"/appointments/{booked['id']}/pre-visit-summary", headers=patient_headers
    )

    assert response.status_code == 403


async def test_another_doctor_cannot_read_the_brief(
    client: AsyncClient,
    patient_headers: Headers,
    bookable_doctor: DoctorProfile,
    make_doctor: Callable[..., Awaitable[DoctorProfile]],
    db_session: AsyncSession,
    auth_header: Callable[[User], Headers],
) -> None:
    booked = await book(client, patient_headers, bookable_doctor, slot_at(a_future_day(), 9, 0))
    other = await make_doctor(specialisation="Dermatology")
    other_user = await db_session.get(User, other.user_id)
    assert other_user is not None

    response = await client.get(
        f"/appointments/{booked['id']}/pre-visit-summary", headers=auth_header(other_user)
    )

    assert response.status_code == 404


# ---------------------------------------------------------------- prompt construction


def test_the_post_visit_prompt_lists_the_prescription_exactly() -> None:
    from app.models.clinical import Prescription, PrescriptionMedication

    prescription = Prescription(
        clinical_notes=NOTES,
        follow_up_date=date(2026, 9, 10),
        issued_at=datetime.now(UTC),
        medications=[
            PrescriptionMedication(
                drug_name="Paracetamol",
                dosage="500mg",
                times_per_day=3,
                duration_days=5,
                instructions="After food",
            )
        ],
    )

    prompt = summaries.build_post_visit_prompt(prescription)

    assert NOTES in prompt
    assert "Paracetamol, 500mg, 3 time(s) per day for 5 day(s)" in prompt
    assert "After food" in prompt
    assert "2026-09-10" in prompt


def test_the_post_visit_prompt_handles_no_medication() -> None:
    from app.models.clinical import Prescription

    prescription = Prescription(clinical_notes=NOTES, issued_at=datetime.now(UTC), medications=[])

    assert "No medication was prescribed." in summaries.build_post_visit_prompt(prescription)


@pytest.mark.parametrize(
    ("urgency_text", "expected"),
    [("Low", "low"), ("MEDIUM", "medium"), (" High ", "high")],
)
def test_urgency_is_normalised_from_the_prompts_wording(urgency_text: str, expected: str) -> None:
    """The prompt asks for "Low / Medium / High"; the database stores lower case."""
    summary = PreVisitSummary(
        urgency=urgency_text,
        chief_complaint="Sore throat",
        suggested_questions=["a", "b", "c"],
    )

    assert summary.urgency == expected


def test_an_urgency_outside_the_permitted_set_is_rejected() -> None:
    with pytest.raises(ValueError, match="urgency must be"):
        PreVisitSummary(
            urgency="catastrophic", chief_complaint="x", suggested_questions=["a", "b", "c"]
        )


def test_the_post_visit_schema_requires_its_three_parts() -> None:
    summary = PostVisitSummary(
        summary="You have a viral sore throat.",
        medication_schedule=["Paracetamol 500mg, three times a day, for five days"],
        follow_up_steps=["Come back if the fever lasts beyond three days"],
    )

    assert summary.medication_schedule
    assert summary.follow_up_steps
