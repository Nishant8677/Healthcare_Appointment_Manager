"""Google Calendar connections and the sync queue.

Two tables with deliberately different characters. `CalendarConnection` holds a credential —
one row per user, guarded like a password. `CalendarSyncJob` holds *desired state* — one row
per (appointment, user), rewritten in place whenever the appointment changes.
"""

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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from app.models.enums import CalendarSyncAction, CalendarSyncStatus

if TYPE_CHECKING:
    from app.models.appointment import Appointment
    from app.models.user import User


class CalendarConnection(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One user's authorisation to write to their Google Calendar.

    The refresh token is stored encrypted, never in plaintext: it is a long-lived key to
    somebody's personal calendar, and a database dump that leaked it would be handing out
    ongoing access rather than a stale password hash.

    Only the *refresh* token is persisted. Access tokens live for an hour and are cached in
    process memory, so the shortest-lived secret is also the one that never touches disk.
    """

    __tablename__ = "calendar_connections"

    # Unique: a second connection for the same user would leave "which calendar do we write
    # to" undefined. Reconnecting updates this row rather than adding another.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    # The Google account that granted access, which is not necessarily the address the user
    # registered with. Shown in the portal so "which calendar is this writing to?" has an
    # answer without a round trip to Google.
    google_account_email: Mapped[str] = mapped_column(String(320), nullable=False)
    calendar_id: Mapped[str] = mapped_column(String(255), nullable=False, server_default="primary")

    encrypted_refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    # Recorded so a later scope change can be detected: an old connection granted narrower
    # access than the code now assumes would otherwise fail as a puzzling 403.
    granted_scope: Mapped[str] = mapped_column(Text, nullable=False)

    # Set when Google reports the grant is gone (the user revoked it in their account
    # settings). The row is kept, not deleted, so the portal can say "reconnect" rather than
    # silently showing a never-connected state.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship()

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def __repr__(self) -> str:
        state = "active" if self.is_active else "revoked"
        return f"<CalendarConnection user={self.user_id} ({state})>"


class CalendarSyncJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """What one user's calendar should show for one appointment.

    This is a reconciler, not a queue of commands. There is at most one row per
    `(appointment_id, user_id)`; enqueueing a change *overwrites* it. That removes an entire
    class of bug: with a command queue, a cancellation raised a millisecond after a booking
    can be delivered first by a second worker, leaving a live calendar event for an
    appointment that no longer exists. Here the last writer simply states the truth, and the
    worker's job is to make Google agree with it.

    `google_event_id` is derived from the appointment and user ids rather than assigned by
    Google, which makes creation idempotent: a request that timed out after Google committed
    it can be retried safely, because the retry addresses the same event instead of making a
    second one.
    """

    __tablename__ = "calendar_sync_jobs"

    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appointments.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Whose calendar. Both participants get their own row, because they have separate
    # connections that can fail independently.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    action: Mapped[CalendarSyncAction] = mapped_column(
        pg_enum(CalendarSyncAction, "calendar_sync_action"), nullable=False
    )
    status: Mapped[CalendarSyncStatus] = mapped_column(
        pg_enum(CalendarSyncStatus, "calendar_sync_status"),
        nullable=False,
        server_default=CalendarSyncStatus.PENDING.value,
    )

    google_event_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    calendar_id: Mapped[str] = mapped_column(String(255), nullable=False, server_default="primary")

    # The event's fields, frozen at enqueue time. Rendering from live rows would mean an
    # event written after a cancellation could describe state the appointment no longer has.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Whether Google is known to hold this event. Decides insert-first or update-first, and
    # lets a delete for an event that was never created finish without a network call.
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # The constraint the whole design rests on: one desired state per calendar per
        # appointment. Without it, "overwrite the pending row" degrades into "append another".
        UniqueConstraint("appointment_id", "user_id", name="uq_calendar_sync_appointment_user"),
        # The worker's claim query. Partial, because settled rows accumulate for the life of
        # the clinic and must not bloat the index the worker reads on every poll.
        Index(
            "ix_calendar_sync_jobs_due",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        CheckConstraint("attempts >= 0", name="calendar_attempts_not_negative"),
    )

    appointment: Mapped[Appointment] = relationship()
    user: Mapped[User] = relationship()

    def __repr__(self) -> str:
        return f"<CalendarSyncJob {self.action} appointment={self.appointment_id} ({self.status})>"
