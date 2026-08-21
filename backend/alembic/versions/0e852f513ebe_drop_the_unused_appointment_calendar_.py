"""drop the unused appointment calendar event id columns

These two columns were added in Phase 7 to hold the Google event ids, so a reschedule or
cancellation could update the right entries instead of orphaning them. The reconciler that
Phase 7 actually shipped solved it differently: `calendar_sync_jobs` carries one row per
calendar per appointment, and the event id lives there, on the row that knows the desired
state. Nothing ever read or wrote these columns - a grep across `src/` and `tests/` finds
only the model declaration itself.

Dead columns are not free. They are the first thing a reader consults when asking where an
event id is kept, and they answer wrongly; and NULL in a column that looks like it should
hold something reads as a bug rather than as a column nobody fills.

Dropping is safe precisely because nothing wrote them: every row holds NULL, so the
downgrade restores the schema exactly, with no data to lose or recover.

Revision ID: 0e852f513ebe
Revises: 6db78daa0931
Create Date: 2026-08-21 15:04:14.995547
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0e852f513ebe"
down_revision: str | None = "6db78daa0931"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("appointments", "doctor_calendar_event_id")
    op.drop_column("appointments", "patient_calendar_event_id")


def downgrade() -> None:
    op.add_column(
        "appointments",
        sa.Column("patient_calendar_event_id", sa.VARCHAR(length=255), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("doctor_calendar_event_id", sa.VARCHAR(length=255), nullable=True),
    )
