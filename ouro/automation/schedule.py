from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ouro.core.tasks import TaskSchedule


def next_occurrence(
    schedule: TaskSchedule, after: datetime, *, inclusive: bool = False
) -> datetime | None:
    """Find the next occurrence without stepping through missed minutes or days.

    Calendar schedules use wall time; nonexistent DST times are skipped and an
    ambiguous time runs once (the first fold). Monthly dates absent in a month
    are skipped. Elapsed minute/hour repeats remain anchored to their start.
    """

    if after.tzinfo is None:
        raise ValueError("Schedule comparison requires an aware datetime")
    threshold = after.astimezone(UTC) + (timedelta() if inclusive else timedelta(microseconds=1))
    threshold = max(threshold, schedule.starts_at.astimezone(UTC))
    if schedule.ends_at is not None and threshold > schedule.ends_at:
        return None
    zone = ZoneInfo(schedule.timezone)
    anchor = schedule.starts_at.astimezone(zone)
    local = threshold.astimezone(zone)
    frequency = schedule.frequency
    unit = (
        schedule.unit
        if frequency == "custom"
        else {
            "hourly": "hours",
            "daily": "days",
            "weekdays": "days",
            "weekly": "weeks",
            "monthly": "months",
        }[frequency]
    )
    interval = schedule.interval if frequency == "custom" else 1
    candidate: datetime | None

    if frequency == "hourly":
        wall = local.replace(tzinfo=None, minute=schedule.at.minute, second=0, microsecond=0)
        # Calendar hours preserve the selected minute across fractional DST
        # changes, and run an ambiguous wall time only in its first fold.
        for offset in range(49):
            try:
                hour = wall + timedelta(hours=offset)
            except OverflowError:
                return None
            candidate = _wall_time(hour.date(), hour.time(), zone)
            if candidate is not None and candidate >= threshold:
                return _within_end(schedule, candidate)
        return None

    if unit in {"minutes", "hours"}:
        first = anchor.astimezone(UTC)
        step = timedelta(minutes=interval) if unit == "minutes" else timedelta(hours=interval)
        offset = max(0, (threshold - first) // step)
        candidate = first + offset * step
        if candidate < threshold:
            candidate += step
        return _within_end(schedule, candidate)

    selected_days = set(schedule.weekdays)
    if frequency == "weekdays":
        selected_days = set(range(5))
    if unit == "weeks" and not selected_days:
        selected_days = {anchor.weekday()}

    if unit == "months":
        first_month = anchor.year * 12 + anchor.month - 1
        local_month = local.year * 12 + local.month - 1
        offset = max(0, (local_month - first_month) // interval)
        day = schedule.month_day or anchor.day
        # Gregorian date/weekday combinations repeat every 4800 months.
        for index in range(offset, offset + 4801):
            month_index = first_month + index * interval
            year, month_zero = divmod(month_index, 12)
            if year > 9999:
                return None
            month = month_zero + 1
            if day > calendar.monthrange(year, month)[1]:
                continue
            candidate_date = date(year, month, day)
            if selected_days and candidate_date.weekday() not in selected_days:
                continue
            candidate = _wall_time(candidate_date, schedule.at, zone)
            if candidate is not None and candidate >= threshold:
                return _within_end(schedule, candidate)
        return None

    start_date = anchor.date()
    if unit == "weeks":
        start_date -= timedelta(days=start_date.weekday())
        step_days = 7 * interval
    else:
        step_days = interval
    offset = max(0, (local.date() - start_date).days // step_days)
    # Seven periods cover every possible weekday residue for day repeats.
    # Extra periods allow one nonexistent DST occurrence to be skipped.
    for index in range(offset, offset + 16):
        try:
            base = start_date + timedelta(days=index * step_days)
        except OverflowError:
            return None
        days = range(7) if unit == "weeks" else (0,)
        for delta in days:
            try:
                candidate_date = base + timedelta(days=delta)
            except OverflowError:
                return None
            if selected_days and candidate_date.weekday() not in selected_days:
                continue
            candidate = _wall_time(candidate_date, schedule.at, zone)
            if candidate is not None and candidate >= threshold:
                return _within_end(schedule, candidate)
    return None


def _wall_time(day: date, at: time, zone: ZoneInfo) -> datetime | None:
    wall = datetime.combine(day, at)
    candidate = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
    return candidate if candidate.astimezone(zone).replace(tzinfo=None) == wall else None


def _within_end(schedule: TaskSchedule, candidate: datetime) -> datetime | None:
    return candidate if schedule.ends_at is None or candidate <= schedule.ends_at else None
