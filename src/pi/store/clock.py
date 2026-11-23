"""The time conventions of architecture §8, in one place.

Every stored timestamp is timezone-aware UTC. Trading-day conventions are defined in IST.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
PUBLISH_TIME_IST = time(18, 0)  # bhavcopy for D is treated as known at 18:00 IST on D


def utc_now() -> datetime:
    return datetime.now(UTC)


def ist_at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=IST).astimezone(UTC)


def known_at_convention(d: date) -> datetime:
    """When day D's exchange file is treated as known (18:00 IST on D), in UTC."""
    return ist_at(d, PUBLISH_TIME_IST)


def decision_cutoff(d: date) -> datetime:
    """The latest instant that counts as 'the evening of D' (end of D in IST), in UTC.

    Queries made for decisions on D may only see rows with known_at <= this instant.
    """
    return ist_at(d + timedelta(days=1), time(0, 0)) - timedelta(microseconds=1)


def latest_due_trade_date(now: datetime) -> date:
    """The newest trade date whose files should exist by `now` (today after 18:00 IST)."""
    local = now.astimezone(IST)
    if local.time() >= PUBLISH_TIME_IST:
        return local.date()
    return local.date() - timedelta(days=1)


def require_aware(ts: datetime) -> datetime:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"naive datetime not allowed: {ts!r}")
    return ts.astimezone(UTC)
