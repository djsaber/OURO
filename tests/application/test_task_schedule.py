from __future__ import annotations

from datetime import UTC, datetime, time

import pytest
from pydantic import ValidationError

from ouro.automation.schedule import next_occurrence
from ouro.core.tasks import TaskSchedule


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


@pytest.mark.parametrize(
    ("values", "after", "expected"),
    [
        ({"frequency": "hourly", "at": time(9, 15)}, "2026-09-07T09:16", "2026-09-07T10:15"),
        ({"frequency": "daily", "at": time(10)}, "2026-09-07T10:00", "2026-09-08T10:00"),
        ({"frequency": "weekdays"}, "2026-09-11T09:00", "2026-09-14T09:00"),
        ({"frequency": "weekly", "weekdays": (0, 4)}, "2026-09-07T09:00", "2026-09-11T09:00"),
        ({"frequency": "monthly", "month_day": 31}, "2026-09-07T09:00", "2026-10-31T09:00"),
        (
            {"frequency": "custom", "unit": "minutes", "interval": 20},
            "2026-09-07T09:01",
            "2026-09-07T09:20",
        ),
        (
            {"frequency": "custom", "unit": "hours", "interval": 3},
            "2026-09-07T09:00",
            "2026-09-07T12:00",
        ),
        (
            {"frequency": "custom", "unit": "days", "interval": 2},
            "2026-09-07T09:00",
            "2026-09-09T09:00",
        ),
        (
            {"frequency": "custom", "unit": "weeks", "interval": 2, "weekdays": (0, 2)},
            "2026-09-09T09:00",
            "2026-09-21T09:00",
        ),
        (
            {"frequency": "custom", "unit": "months", "interval": 3},
            "2026-09-07T09:00",
            "2026-12-07T09:00",
        ),
        (
            {"frequency": "daily", "timezone": "Asia/Shanghai", "at": time(19)},
            "2026-09-07T09:00",
            "2026-09-07T11:00",
        ),
    ],
)
def test_calendar_and_elapsed_recurrences(values, after, expected):
    schedule = TaskSchedule(starts_at=dt("2026-09-07T09:00"), **values)
    assert next_occurrence(schedule, dt(after)) == dt(expected)


def test_exact_boundary_and_end_date_are_inclusive_only_when_requested():
    start = dt("2026-09-07T09:00")
    schedule = TaskSchedule(frequency="daily", starts_at=start, ends_at=start)
    assert next_occurrence(schedule, start, inclusive=True) == start
    assert next_occurrence(schedule, start) is None


def test_monthly_invalid_date_skips_to_leap_year_without_clamping():
    schedule = TaskSchedule(
        frequency="custom",
        unit="months",
        interval=12,
        month_day=29,
        starts_at=dt("2025-02-01T09:00"),
    )
    assert next_occurrence(schedule, schedule.starts_at) == dt("2028-02-29T09:00")


def test_dst_nonexistent_calendar_time_is_skipped_and_fold_executes_once():
    spring = TaskSchedule(
        frequency="daily",
        starts_at=dt("2026-03-01T00:00"),
        timezone="America/New_York",
        at=time(2, 30),
    )
    assert next_occurrence(spring, dt("2026-03-07T07:30")) == dt("2026-03-09T06:30")
    autumn = TaskSchedule(
        frequency="daily",
        starts_at=dt("2026-10-01T00:00"),
        timezone="America/New_York",
        at=time(1, 30),
    )
    assert next_occurrence(autumn, dt("2026-11-01T04:00")) == dt("2026-11-01T05:30")
    assert next_occurrence(autumn, dt("2026-11-01T05:30")) == dt("2026-11-02T06:30")


def test_elapsed_recurrence_skips_decades_in_constant_work():
    schedule = TaskSchedule(
        frequency="custom",
        unit="minutes",
        interval=5,
        starts_at=dt("2000-01-01T00:00"),
    )
    assert next_occurrence(schedule, dt("2090-01-01T00:02")) == dt("2090-01-01T00:05")


@pytest.mark.parametrize(
    ("timezone", "after", "expected"),
    [
        ("Australia/Lord_Howe", "2026-04-04T14:16", "2026-04-04T15:45"),
        ("America/New_York", "2026-11-01T05:16", "2026-11-01T07:15"),
        ("America/New_York", "2026-03-08T06:16", "2026-03-08T07:15"),
    ],
)
def test_hourly_wall_time_preserves_minute_and_skips_repeated_hours(timezone, after, expected):
    schedule = TaskSchedule(
        frequency="hourly",
        starts_at=dt("2026-01-01T00:00"),
        timezone=timezone,
        at=time(0, 15),
    )
    assert next_occurrence(schedule, dt(after)) == dt(expected)


@pytest.mark.parametrize(
    "changes",
    [
        {"timezone": "Not/AZone"},
        {"starts_at": datetime(2026, 9, 7)},
        {"ends_at": dt("2025-01-01")},
        {"interval": 0},
        {"interval": True},
        {"frequency": "weekly", "weekdays": (0, 0)},
        {"frequency": "weekly", "weekdays": (7,)},
        {"at": time(1, 2, 3)},
        {"frequency": "daily", "month_day": 12},
        {"frequency": "custom", "unit": "hours", "weekdays": (0,)},
        {"unknown_field": True},
    ],
)
def test_invalid_schedule_has_a_diagnostic_validation_error(changes):
    with pytest.raises(ValidationError):
        TaskSchedule.model_validate(
            {"frequency": "daily", "starts_at": dt("2026-09-07T09:00"), **changes}
        )
