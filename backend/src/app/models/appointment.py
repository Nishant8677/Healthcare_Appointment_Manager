"""Appointments and the symptom form a patient submits before confirming."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from app.models.enums import AppointmentStatus

if TYPE_CHECKING:
    from app.models.clinical import AiSummary, Prescription
    from app.models.doctor import DoctorProfile
    from app.models.user import User

# Statuses that occupy a slot, as a SQL literal list for the partial index below. Kept in
# sync with `OCCUPYING_STATUSES` by a test, since a silent divergence here would quietly
# disable double-booking protection.
_OCCUPYING_SQL = "('held', 'confirmed', 'completed')"


class Appointment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One booking of one slot with one doctor.

    Double-booking protection has two layers. The booking service takes a row lock and
    re-checks availability inside the transaction; the partial unique index below is the
    guarantee that holds even if that logic were wrong, because the database itself will
    reject a second occupying row for the same doctor and start time.
    """

    __tablename__ = "appointments"

    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    doctor_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("doctor_profiles.id", ondelete="RESTRICT"),
        nullable=False,
    )

    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[AppointmentStatus] = mapped_column(
        pg_enum(AppointmentStatus, "appointment_status"),
        nullable=False,
        server_default=AppointmentStatus.HELD.value,
    )

    # Set while the patient completes the symptom form. An expired hold is treated as free
    # by the availability query, so no sweeper job is needed to reclaim abandoned slots.
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index(
            "uq_appointments_doctor_occupied_slot",
            "doctor_profile_id",
            "starts_at",
            unique=True,
            postgresql_where=text(f"status IN {_OCCUPYING_SQL}"),
        ),
        # Supports "my upcoming appointments" and the doctor's daily schedule.
        Index("ix_appointments_patient_starts_at", "patient_id", "starts_at"),
        Index("ix_appointments_doctor_starts_at", "doctor_profile_id", "starts_at"),
        CheckConstraint("ends_at > starts_at", name="appointment_ends_after_it_starts"),
        CheckConstraint(
            "status <> 'held' OR hold_expires_at IS NOT NULL",
            name="held_appointment_has_expiry",
        ),
    )

    patient: Mapped[User] = relationship(
        back_populates="appointments_as_patient",
        foreign_keys=[patient_id],
    )
    doctor_profile: Mapped[DoctorProfile] = relationship(back_populates="appointments")
    symptom_report: Mapped[SymptomReport | None] = relationship(
        back_populates="appointment",
        cascade="all, delete-orphan",
        uselist=False,
    )
    ai_summaries: Mapped[list[AiSummary]] = relationship(
        back_populates="appointment",
        cascade="all, delete-orphan",
    )
    prescription: Mapped[Prescription | None] = relationship(
        back_populates="appointment",
        cascade="all, delete-orphan",
        uselist=False,
    )

    def __repr__(self) -> str:
        return f"<Appointment {self.starts_at.isoformat()} ({self.status})>"


class SymptomReport(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """What the patient described before confirming the appointment.

    Kept separate from `appointments` because it is free text supplied by the patient and is
    the input to the pre-visit LLM summary — it must be preserved verbatim, independently of
    whatever the model later made of it.
    """

    __tablename__ = "symptom_reports"

    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appointments.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    symptoms: Mapped[str] = mapped_column(Text, nullable=False)
    duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    additional_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "duration_days IS NULL OR duration_days >= 0",
            name="symptom_duration_not_negative",
        ),
    )

    appointment: Mapped[Appointment] = relationship(back_populates="symptom_report")

    def __repr__(self) -> str:
        return f"<SymptomReport appointment={self.appointment_id}>"
