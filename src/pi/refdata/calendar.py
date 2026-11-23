"""Trading calendar (WP 1.1, architecture §7 and review A10).

A day is a trading day if its bhavcopy exists, which covers Muhurat and special Saturday
sessions. The holiday list only separates "holiday" from "file missing" on weekdays.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta


def _days(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def expected_trading_days(start: date, end: date, holidays: Iterable[date]) -> list[date]:
    """Weekdays in [start, end] that are not listed holidays."""
    off = set(holidays)
    return [d for d in _days(start, end) if d.weekday() < 5 and d not in off]


def trading_days(
    start: date, end: date, holidays: Iterable[date], present: Iterable[date]
) -> list[date]:
    """Expected weekdays plus any day whose file exists (special sessions), in order."""
    extra = {d for d in present if start <= d <= end}
    return sorted(set(expected_trading_days(start, end, holidays)) | extra)


def missing_days(
    start: date, end: date, holidays: Iterable[date], present: Iterable[date]
) -> list[date]:
    """Expected trading days in [start, end] with no file."""
    have = set(present)
    return [d for d in expected_trading_days(start, end, holidays) if d not in have]


def clean_streak(
    through: date, ok_days: Iterable[date], holidays: Iterable[date], present: Iterable[date]
) -> int:
    """Consecutive trading days ending at `through` that all completed cleanly.

    Computed fresh from the run log and the current calendar each time, so adding a
    forgotten holiday to calendar.yaml repairs the count instead of leaving it reset.
    """
    ok = set(ok_days)
    if not ok or through not in ok:
        return 0
    days = trading_days(min(ok), through, holidays, present)
    streak = 0
    for d in reversed(days):
        if d not in ok:
            break
        streak += 1
    return streak
