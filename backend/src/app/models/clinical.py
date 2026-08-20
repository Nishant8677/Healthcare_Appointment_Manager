"""LLM-generated summaries, prescriptions and their medications."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from app.models.enums import SummaryStatus, SummaryType, UrgencyLevel

if TYPE_CHECKING:
    from app.models.appointment import Appointment


class AiSummary(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One model-generated summary attached to an appointment.

    Stored rather than regenerated on read: the output is part of the clinical record, and
    re-running the prompt could produce different text for the same visit. `prompt_version`
    and `model` are recorded so an old summary can always be explained by the exact prompt
    that produced it.
    """

    __tablename__ = "ai_summaries"

    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appointments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    summary_type: Mapped[SummaryType] = mapped_column(
        pg_enum(SummaryType, "summary_type"), nullable=False
    )
    status: Mapped[SummaryStatus] = mapped_column(
        pg_enum(SummaryStatus, "summary_status"),
        nullable=False,
        server_default=SummaryStatus.PENDING.value,
    )

    # Only meaningful for a pre-visit summary; a post-visit summary leaves it null.
    urgency: Mapped[UrgencyLevel | None] = mapped_column(
        pg_enum(UrgencyLevel, "urgency_level"), nullable=True
    )

    # The validated model output. JSONB rather than columns because the two summary types
    # have different shapes (chief complaint and suggested questions, versus medication
    # schedule and follow-up steps) and both are validated against a schema before storage.
    content: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("appointment_id", "summary_type", name="uq_ai_summaries_per_type"),
        CheckConstraint(
            "status <> 'ready' OR content IS NOT NULL",
            name="ready_summary_has_content",
        ),
        CheckConstraint("attempts >= 0", name="ai_summary_attempts_not_negative"),
    )

    appointment: Mapped[Appointment] = relationship(back_populates="ai_summaries")

    def __repr__(self) -> str:
        return f"<AiSummary {self.summary_type} ({self.status})>"


class Prescription(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """The doctor's record of a completed visit."""

    __tablename__ = "prescriptions"

    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appointments.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    # Raw notes as the doctor wrote them; the patient-facing rewrite lives in `ai_summaries`
    # so the clinical original is never overwritten by generated text.
    clinical_notes: Mapped[str] = mapped_column(Text, nullable=False)
    follow_up_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    appointment: Mapped[Appointment] = relationship(back_populates="prescription")
    medications: Mapped[list[PrescriptionMedication]] = relationship(
        back_populates="prescription",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Prescription appointment={self.appointment_id}>"


class PrescriptionMedication(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One prescribed medicine, as structured data.

    Deliberately structured rather than free text: medication reminders are scheduled from
    `times_per_day` and `duration_days`, and a reminder schedule must never depend on
    parsing an LLM's prose.
    """

    __tablename__ = "prescription_medications"

    prescription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("prescriptions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    drug_name: Mapped[str] = mapped_column(String(200), nullable=False)
    dosage: Mapped[str] = mapped_column(String(100), nullable=False)
    times_per_day: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    instructions: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "times_per_day > 0 AND times_per_day <= 12",
            name="medication_frequency_within_bounds",
        ),
        CheckConstraint(
            "duration_days > 0 AND duration_days <= 365",
            name="medication_duration_within_bounds",
        ),
    )

    prescription: Mapped[Prescription] = relationship(back_populates="medications")

    def __repr__(self) -> str:
        return f"<PrescriptionMedication {self.drug_name}>"
