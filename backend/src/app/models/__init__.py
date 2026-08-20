"""SQLAlchemy models.

Every model module must be imported here: Alembic autogenerate only sees tables that have
been registered on `Base.metadata` by import time.
"""

from app.models.appointment import Appointment, SymptomReport
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.calendar import CalendarConnection, CalendarSyncJob
from app.models.clinical import AiSummary, Prescription, PrescriptionMedication
from app.models.doctor import DoctorLeaveDay, DoctorProfile, DoctorWorkingHours
from app.models.enums import (
    OCCUPYING_STATUSES,
    AppointmentStatus,
    CalendarSyncAction,
    CalendarSyncStatus,
    NotificationStatus,
    NotificationType,
    SummaryStatus,
    SummaryType,
    UrgencyLevel,
    UserRole,
)
from app.models.notification import NotificationJob
from app.models.user import User

__all__ = [
    "OCCUPYING_STATUSES",
    "AiSummary",
    "Appointment",
    "AppointmentStatus",
    "Base",
    "CalendarConnection",
    "CalendarSyncAction",
    "CalendarSyncJob",
    "CalendarSyncStatus",
    "DoctorLeaveDay",
    "DoctorProfile",
    "DoctorWorkingHours",
    "NotificationJob",
    "NotificationStatus",
    "NotificationType",
    "Prescription",
    "PrescriptionMedication",
    "SummaryStatus",
    "SummaryType",
    "SymptomReport",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "UrgencyLevel",
    "User",
    "UserRole",
]
