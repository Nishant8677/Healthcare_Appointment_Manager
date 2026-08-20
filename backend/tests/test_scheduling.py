"""Scheduling rules, tested as pure functions.

No database and no HTTP: slot arithmetic is the most error-prone part of the system, so it
is exercised directly and exhaustively here rather than through an endpoint.
"""

from __future__ import annotations

from datetime import time

import pytest

from app.core.exceptions import InvalidSchedule
from app.services.scheduling import (
    WorkingWindow,
    format_minutes,
    slot_count,
    validate_weekly_schedule,
)

MONDAY, TUESDAY = 0, 1


def window(weekday: int, start: str, end: str) -> WorkingWindow:
    hour, minute = (int(part) for part in start.split(":"))
    end_hour, end_minute = (int(part) for part in end.split(":"))
    return WorkingWindow(weekday=weekday, start=time(hour, minute), end=time(end_hour, end_minute))


# ---------------------------------------------------------------- valid schedules


def test_a_schedule_that_divides_evenly_is_accepted() -> None:
    validate_weekly_schedule([window(MONDAY, "09:00", "17:00")], slot_duration_minutes=30)


def test_an_empty_schedule_is_valid() -> None:
    """A doctor may exist before their hours are set."""
    validate_weekly_schedule([], slot_duration_minutes=30)


def test_a_split_day_is_allowed() -> None:
    """Morning and evening clinic with a break between them."""
    validate_weekly_schedule(
        [window(MONDAY, "09:00", "12:00"), window(MONDAY, "15:00", "18:00")],
        slot_duration_minutes=30,
    )


def test_windows_that_touch_exactly_are_not_an_overlap() -> None:
    """One ending precisely when the next begins is a continuous day, not a clash."""
    validate_weekly_schedule(
        [window(MONDAY, "09:00", "12:00"), window(MONDAY, "12:00", "17:00")],
        slot_duration_minutes=60,
    )


def test_the_same_hours_on_different_days_do_not_clash() -> None:
    validate_weekly_schedule(
        [window(MONDAY, "09:00", "17:00"), window(TUESDAY, "09:00", "17:00")],
        slot_duration_minutes=30,
    )


# ---------------------------------------------------------------- rejected schedules


def test_overlapping_windows_are_rejected() -> None:
    with pytest.raises(InvalidSchedule) as error:
        validate_weekly_schedule(
            [window(MONDAY, "09:00", "13:00"), window(MONDAY, "12:00", "17:00")],
            slot_duration_minutes=60,
        )

    message = str(error.value)
    assert "Monday" in message
    assert "overlapping" in message.lower()


def test_overlap_is_detected_even_when_windows_arrive_out_of_order() -> None:
    """Validation must not depend on the caller sorting the input."""
    with pytest.raises(InvalidSchedule):
        validate_weekly_schedule(
            [window(MONDAY, "14:00", "18:00"), window(MONDAY, "09:00", "15:00")],
            slot_duration_minutes=60,
        )


def test_a_window_shorter_than_one_appointment_is_rejected() -> None:
    with pytest.raises(InvalidSchedule) as error:
        validate_weekly_schedule([window(MONDAY, "09:00", "09:20")], slot_duration_minutes=30)

    assert "shorter than one" in str(error.value)


def test_a_window_that_does_not_divide_evenly_suggests_valid_end_times() -> None:
    """The error hands the admin the fix instead of making them do the arithmetic."""
    with pytest.raises(InvalidSchedule) as error:
        validate_weekly_schedule([window(MONDAY, "09:00", "17:00")], slot_duration_minutes=45)

    message = str(error.value)
    assert "480 minutes" in message
    assert "16:30" in message  # ten 45-minute appointments
    assert "17:15" in message  # eleven


def test_working_hours_must_fall_on_whole_minutes() -> None:
    stray_seconds = WorkingWindow(weekday=MONDAY, start=time(9, 0, 30), end=time(17, 0))

    with pytest.raises(InvalidSchedule) as error:
        validate_weekly_schedule([stray_seconds], slot_duration_minutes=30)

    assert "whole minutes" in str(error.value)


@pytest.mark.parametrize("bad_duration", [0, -30])
def test_a_non_positive_slot_duration_is_rejected(bad_duration: int) -> None:
    with pytest.raises(InvalidSchedule):
        validate_weekly_schedule([window(MONDAY, "09:00", "17:00")], bad_duration)


# ---------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("start", "end", "duration", "expected"),
    [
        ("09:00", "17:00", 30, 16),
        ("09:00", "17:00", 60, 8),
        ("09:00", "09:30", 30, 1),
        ("10:00", "13:00", 20, 9),
    ],
)
def test_slot_count(start: str, end: str, duration: int, expected: int) -> None:
    assert slot_count(window(MONDAY, start, end), duration) == expected


@pytest.mark.parametrize(
    ("minutes", "expected"), [(0, "00:00"), (540, "09:00"), (1005, "16:45"), (1439, "23:59")]
)
def test_format_minutes(minutes: int, expected: str) -> None:
    assert format_minutes(minutes) == expected
