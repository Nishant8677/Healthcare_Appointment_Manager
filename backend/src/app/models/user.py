"""User accounts and authentication identity."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from app.models.enums import UserRole

if TYPE_CHECKING:
    from app.models.appointment import Appointment
    from app.models.doctor import DoctorProfile


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A person who can sign in, in any of the three roles.

    One table rather than three: authentication, password reset and notification addressing
    are identical for all roles, and role-specific data lives in its own table
    (`doctor_profiles`) rather than as columns that are null for two thirds of rows.
    """

    __tablename__ = "users"

    # Stored lower-cased so the unique constraint is effectively case-insensitive without
    # requiring the citext extension, which is not available on every managed provider.
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[UserRole] = mapped_column(pg_enum(UserRole, "user_role"), nullable=False)

    # Deactivation rather than deletion: appointments and prescriptions must remain
    # attributable after a person leaves the clinic.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    doctor_profile: Mapped[DoctorProfile | None] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False,
    )
    appointments_as_patient: Mapped[list[Appointment]] = relationship(
        back_populates="patient",
        foreign_keys="Appointment.patient_id",
    )

    def __repr__(self) -> str:
        return f"<User {self.email} ({self.role})>"
