"""Wall, monotonic and virtual clocks."""

from __future__ import annotations

import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MILLISECONDS_PER_DAY = 24 * 60 * 60 * 1000


class WallClock(Protocol):
    def now_utc(self) -> datetime:
        ...


class MonotonicClock(Protocol):
    def now_ns(self) -> int:
        ...


def require_aware_utc(value: datetime) -> datetime:
    """Validate a timezone-aware datetime and normalize it to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime is not allowed")
    return value.astimezone(timezone.utc)


def parse_clock_time_ms(value: str, *, allow_day_end: bool = False) -> int:
    """Parse strict ``HH:MM:SS`` into milliseconds since local midnight.

    ``24:00:00`` is accepted only when ``allow_day_end`` is true and is meant
    for an exclusive interval endpoint, never an actual instant in a date.
    """

    if not isinstance(value, str):
        raise TypeError("clock value must be strict HH:MM:SS text")
    if len(value) != 8 or value[2] != ":" or value[5] != ":":
        raise ValueError("clock value must be strict HH:MM:SS")
    parts = value.split(":")
    if any(len(part) != 2 or not part.isdigit() for part in parts):
        raise ValueError("clock value must be strict HH:MM:SS")
    hour, minute, second = (int(part) for part in parts)
    if allow_day_end and (hour, minute, second) == (24, 0, 0):
        return MILLISECONDS_PER_DAY
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or not 0 <= second <= 59:
        raise ValueError("clock value is outside one day")
    return ((hour * 60 + minute) * 60 + second) * 1000


def local_datetime_ms(
    trade_date: str,
    clock_time: str,
    *,
    timezone_name: str = "Asia/Shanghai",
) -> int:
    """Convert one strict local date/time to UTC epoch milliseconds."""

    if not isinstance(trade_date, str):
        raise TypeError("trade_date must be strict YYYY-MM-DD text")
    try:
        parsed_date = date.fromisoformat(trade_date)
    except ValueError as exc:
        raise ValueError("trade_date must be strict valid YYYY-MM-DD") from exc
    if parsed_date.isoformat() != trade_date:
        raise ValueError("trade_date must be strict valid YYYY-MM-DD")
    offset_ms = parse_clock_time_ms(clock_time)
    try:
        local_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown local timezone") from exc
    seconds = offset_ms // 1000
    local = datetime.combine(
        parsed_date,
        datetime_time(
            hour=seconds // 3600,
            minute=(seconds % 3600) // 60,
            second=seconds % 60,
        ),
        tzinfo=local_timezone,
    )
    return int(local.astimezone(timezone.utc).timestamp() * 1000)


class SystemWallClock:
    """Production wall clock."""

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


class SystemMonotonicClock:
    """Production monotonic clock for durations and deadlines."""

    def now_ns(self) -> int:
        return time.monotonic_ns()


class VirtualClock:
    """Deterministic clock for tests and event-time replay."""

    def __init__(self, initial_utc: datetime) -> None:
        self._utc = require_aware_utc(initial_utc)
        self._monotonic_ns = 0

    def now_utc(self) -> datetime:
        return self._utc

    def now_ns(self) -> int:
        return self._monotonic_ns

    def advance_to(self, utc_time: datetime) -> None:
        target = require_aware_utc(utc_time)
        delta = target - self._utc
        if delta.total_seconds() < 0:
            raise ValueError("virtual clock cannot move backwards")
        self._utc = target
        self._monotonic_ns += int(delta.total_seconds() * 1_000_000_000)

    def advance_by(self, duration: timedelta) -> None:
        if duration.total_seconds() < 0:
            raise ValueError("virtual clock cannot move backwards")
        self.advance_to(self._utc + duration)
