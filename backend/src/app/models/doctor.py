"""Doctor profile, recurring availability and leave."""

from __future__ import annotations

import uuid
from datetime import date, time
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.appointment import Appointment
    from app.models.user import User


class DoctorProfile(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Clinic-managed details for one doctor.

    Created by an admin, never by self-registration.
    """

    __tablename__ = "doctor_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    specialisation: Mapped[str] = mapped_column(String(120), nullable=False, index=True)

    # Drives slot generation: a working window is divided into blocks of this length.
    slot_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "slot_duration_minutes > 0 AND slot_duration_minutes <= 240",
            name="slot_duration_within_bounds",
        ),
    )

    user: Mapped[User] = relationship(back_populates="doctor_profile")
    working_hours: Mapped[list[DoctorWorkingHours]] = relationship(
        back_populates="doctor_profile",
        cascade="all, delete-orphan",
    )
    leave_days: Mapped[list[DoctorLeaveDay]] = relationship(
        back_populates="doctor_profile",
        cascade="all, delete-orphan",
    )
    appointments: Mapped[list[Appointment]] = relationship(back_populates="doctor_profile")

    def __repr__(self) -> str:
        return f"<DoctorProfile {self.specialisation}>"


class DoctorWorkingHours(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One recurring window of availability on a given weekday.

    Multiple rows per weekday are allowed so a split day (morning and evening clinic with a
    break between) is expressible without a special case.
    """

    __tablename__ = "doctor_working_hours"

    doctor_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("doctor_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 0 = Monday through 6 = Sunday, matching `datetime.date.weekday()`.
    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "doctor_profile_id",
            "weekday",
            "start_time",
            name="uq_doctor_working_hours_slot_start",
        ),
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_within_week"),
        CheckConstraint("end_time > start_time", name="window_ends_after_it_starts"),
    )

    doctor_profile: Mapped[DoctorProfile] = relationship(back_populates="working_hours")

    def __repr__(self) -> str:
        return f"<DoctorWorkingHours weekday={self.weekday} {self.start_time}-{self.end_time}>"


class DoctorLeaveDay(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A full day on which the doctor is unavailable.

    Whole days only: the assignment describes leave per date, and per-hour absence is better
    expressed by editing working hours than by a second overlapping mechanism.
    """

    __tablename__ = "doctor_leave_days"

    doctor_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("doctor_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    leave_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        UniqueConstraint("doctor_profile_id", "leave_date", name="uq_doctor_leave_days_date"),
    )

    doctor_profile: Mapped[DoctorProfile] = relationship(back_populates="leave_days")

    def __repr__(self) -> str:
        return f"<DoctorLeaveDay {self.leave_date}>"
