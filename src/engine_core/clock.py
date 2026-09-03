"""Wall, monotonic and virtual clocks."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol


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
