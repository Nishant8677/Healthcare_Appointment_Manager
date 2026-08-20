"""The notification outbox."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from app.models.enums import NotificationStatus, NotificationType

if TYPE_CHECKING:
    from app.models.appointment import Appointment
    from app.models.user import User


class NotificationJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One message queued for delivery — the transactional outbox.

    Nothing is ever sent during the request that caused it. A booking writes its
    confirmation rows in the *same* transaction that creates the appointment, so the two
    cannot disagree: if the booking rolls back the notification disappears with it, and if
    the booking commits the notification is guaranteed to be queued. A background worker
    then delivers them, retrying with growing backoff.
    """

    __tablename__ = "notification_jobs"

    notification_type: Mapped[NotificationType] = mapped_column(
        pg_enum(NotificationType, "notification_type"), nullable=False
    )
    status: Mapped[NotificationStatus] = mapped_column(
        pg_enum(NotificationStatus, "notification_status"),
        nullable=False,
        server_default=NotificationStatus.PENDING.value,
    )

    # Captured at enqueue time rather than resolved at send time: if the user later changes
    # their address, the message still goes where it was addressed when it was raised.
    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False)
    recipient_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appointments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Everything the template needs, denormalised so rendering never depends on rows that
    # may have changed (or been cancelled) since the message was raised.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # Reminders are queued immediately but must not go out until their due time.
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # The worker's claim query: pending rows that are due, oldest first. Partial, because
        # sent rows accumulate indefinitely and must not bloat the index the worker uses.
        Index(
            "ix_notification_jobs_due",
            "scheduled_for",
            postgresql_where=text("status = 'pending'"),
        ),
        CheckConstraint("attempts >= 0", name="notification_attempts_not_negative"),
        CheckConstraint(
            "status <> 'sent' OR sent_at IS NOT NULL",
            name="sent_notification_has_timestamp",
        ),
    )

    recipient: Mapped[User | None] = relationship(foreign_keys=[recipient_user_id])
    appointment: Mapped[Appointment | None] = relationship()

    def __repr__(self) -> str:
        return f"<NotificationJob {self.notification_type} ({self.status})>"
