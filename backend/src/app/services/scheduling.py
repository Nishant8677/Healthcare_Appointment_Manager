"""Scheduling rules.

Pure functions over plain values: no database, no HTTP, no ORM. Slot arithmetic is the part
of this system most likely to be subtly wrong, so it is kept where it can be tested
exhaustively and reused by Phase 3's slot generation without dragging a session along.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from itertools import pairwise
from zoneinfo import ZoneInfo

from app.core.exceptions import InvalidSchedule

MINUTES_PER_DAY = 24 * 60
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True, slots=True)
class WorkingWindow:
    """One continuous stretch of availability on a weekday. 0 = Monday, 6 = Sunday."""

    weekday: int
    start: time
    end: time

    @property
    def start_minutes(self) -> int:
        return minutes_since_midnight(self.start)

    @property
    def end_minutes(self) -> int:
        return minutes_since_midnight(self.end)

    @property
    def duration_minutes(self) -> int:
        return self.end_minutes - self.start_minutes


def minutes_since_midnight(value: time) -> int:
    return value.hour * 60 + value.minute


def format_minutes(total_minutes: int) -> str:
    """Render minutes-since-midnight as `HH:MM`, for error messages."""
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours:02d}:{minutes:02d}"


def slot_count(window: WorkingWindow, slot_duration_minutes: int) -> int:
    """How many whole appointments fit in the window."""
    return window.duration_minutes // slot_duration_minutes


def validate_weekly_schedule(windows: Sequence[WorkingWindow], slot_duration_minutes: int) -> None:
    """Check a doctor's complete weekly availability.

    The whole schedule is validated at once rather than window by window, because overlap is
    a property of the set — a single window can never be checked for it in isolation.

    Raises:
        InvalidSchedule: with a message naming the specific window at fault.
    """
    if slot_duration_minutes <= 0:
        raise InvalidSchedule("Slot duration must be a positive number of minutes.")

    for window in windows:
        _validate_window(window, slot_duration_minutes)

    _reject_overlaps(windows)


def _validate_window(window: WorkingWindow, slot_duration_minutes: int) -> None:
    day = _weekday_name(window.weekday)

    if (
        window.start.second
        or window.start.microsecond
        or window.end.second
        or window.end.microsecond
    ):
        raise InvalidSchedule(
            f"{day} {window.start}-{window.end}: working hours must fall on whole minutes."
        )

    if window.duration_minutes <= 0:
        raise InvalidSchedule(f"{day} {_span(window)}: a working window must end after it starts.")

    if window.duration_minutes < slot_duration_minutes:
        raise InvalidSchedule(
            f"{day} {_span(window)} is {window.duration_minutes} minutes, which is shorter "
            f"than one {slot_duration_minutes}-minute appointment."
        )

    remainder = window.duration_minutes % slot_duration_minutes
    if remainder:
        raise InvalidSchedule(
            f"{day} {_span(window)} is {window.duration_minutes} minutes, which is not a "
            f"whole number of {slot_duration_minutes}-minute appointments. "
            f"{_suggest_end_times(window, slot_duration_minutes)}"
        )


def _reject_overlaps(windows: Sequence[WorkingWindow]) -> None:
    """Reject two windows on the same weekday that share any minute.

    Touching windows (one ending exactly when the next begins) are allowed — that is how a
    split clinic day with no break is expressed.
    """
    for weekday, day_windows in _group_by_weekday(windows).items():
        ordered = sorted(day_windows, key=lambda window: window.start_minutes)
        for earlier, later in pairwise(ordered):
            if later.start_minutes < earlier.end_minutes:
                raise InvalidSchedule(
                    f"{_weekday_name(weekday)} has overlapping working hours: "
                    f"{_span(earlier)} and {_span(later)}."
                )


def _group_by_weekday(
    windows: Iterable[WorkingWindow],
) -> dict[int, list[WorkingWindow]]:
    grouped: dict[int, list[WorkingWindow]] = {}
    for window in windows:
        grouped.setdefault(window.weekday, []).append(window)
    return grouped


def _suggest_end_times(window: WorkingWindow, slot_duration_minutes: int) -> str:
    """Name the nearest end times that would divide evenly.

    An error that only says "does not divide evenly" leaves the admin doing arithmetic; this
    hands them the answer.
    """
    whole_slots = slot_count(window, slot_duration_minutes)
    candidates = [
        window.start_minutes + whole_slots * slot_duration_minutes,
        window.start_minutes + (whole_slots + 1) * slot_duration_minutes,
    ]
    valid = [
        format_minutes(candidate)
        for candidate in candidates
        if window.start_minutes < candidate <= MINUTES_PER_DAY
    ]

    if not valid:
        return "No end time later than the start divides evenly within the same day."
    if len(valid) == 1:
        return f"Try ending at {valid[0]}."
    return f"Try ending at {valid[0]} or {valid[1]}."


def slot_starts_for_weekday(
    windows: Iterable[WorkingWindow], slot_duration_minutes: int, weekday: int
) -> list[time]:
    """Every appointment start time on one weekday, in order.

    Pure: takes the doctor's windows and returns wall-clock times. Turning those into real
    instants needs a calendar date and a timezone, which is the caller's job.
    """
    starts: list[time] = []
    for window in windows:
        if window.weekday != weekday:
            continue
        for index in range(slot_count(window, slot_duration_minutes)):
            offset = window.start_minutes + index * slot_duration_minutes
            hours, minutes = divmod(offset, 60)
            starts.append(time(hour=hours, minute=minutes))
    return sorted(starts)


def combine_in_zone(day: date, local_time: time, zone: ZoneInfo) -> datetime | None:
    """Turn a wall-clock time on a date into the UTC instant it refers to.

    Returns `None` when that local time does not exist — the hour skipped by a
    daylight-saving spring-forward. Offering a slot for a moment that never happens would
    produce an appointment nobody could attend, so those are dropped rather than silently
    shifted into a neighbouring hour.
    """
    naive = datetime.combine(day, local_time)
    localised = naive.replace(tzinfo=zone)
    as_utc = localised.astimezone(UTC)

    # Round-trip through the zone: if the wall time came back different, the original local
    # time did not exist on that date.
    if as_utc.astimezone(zone).replace(tzinfo=None) != naive:
        return None
    return as_utc


def _weekday_name(weekday: int) -> str:
    if 0 <= weekday < len(WEEKDAY_NAMES):
        return WEEKDAY_NAMES[weekday]
    return f"Weekday {weekday}"


def _span(window: WorkingWindow) -> str:
    # Plain hyphen rather than an en dash: these strings travel to API clients, where an
    # ASCII-safe separator avoids any encoding surprise.
    return f"{format_minutes(window.start_minutes)}-{format_minutes(window.end_minutes)}"
