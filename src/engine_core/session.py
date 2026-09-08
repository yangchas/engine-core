"""Deterministic trading-session phase primitives.

The calendar decides whether a date is tradable.  A session plan only maps
times within one already-validated trading date to runtime phases.  It never
consults the machine date, guesses a nearby trading day, or schedules work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .calendar import CalendarCoverageError, TradingCalendarSnapshot, parse_trade_date
from .contracts import semantic_hash


SESSION_PLAN_CONTRACT_VERSION = "SessionPlanV1"
MILLISECONDS_PER_DAY = 24 * 60 * 60 * 1000


def _parse_clock_ms(value: str, *, allow_day_end: bool = False) -> int:
    """Parse strict ``HH:MM:SS`` into milliseconds since local midnight."""

    if not isinstance(value, str):
        raise TypeError("session clock value must be strict HH:MM:SS text")
    if len(value) != 8 or value[2] != ":" or value[5] != ":":
        raise ValueError("session clock value must be strict HH:MM:SS")
    parts = value.split(":")
    if any(len(part) != 2 or not part.isdigit() for part in parts):
        raise ValueError("session clock value must be strict HH:MM:SS")
    hour, minute, second = (int(part) for part in parts)
    if allow_day_end and (hour, minute, second) == (24, 0, 0):
        return MILLISECONDS_PER_DAY
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or not 0 <= second <= 59:
        raise ValueError("session clock value is outside one day")
    return ((hour * 60 + minute) * 60 + second) * 1000


@dataclass(frozen=True)
class SessionInterval:
    """One named half-open local-time interval."""

    phase: str
    start_time: str
    end_exclusive_time: str
    start_ms_of_day: int = field(init=False)
    end_exclusive_ms_of_day: int = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.phase, str) or not self.phase.strip():
            raise ValueError("session phase is required")
        start = _parse_clock_ms(self.start_time)
        end = _parse_clock_ms(self.end_exclusive_time, allow_day_end=True)
        if start >= end:
            raise ValueError("session interval must be non-empty and half-open")
        object.__setattr__(self, "phase", self.phase.strip().upper())
        object.__setattr__(self, "start_ms_of_day", start)
        object.__setattr__(self, "end_exclusive_ms_of_day", end)

    def contains_ms_of_day(self, value: int) -> bool:
        return self.start_ms_of_day <= value < self.end_exclusive_ms_of_day


@dataclass(frozen=True)
class SessionPlan:
    """Full-day phase authority for one explicit trading date."""

    trade_date: str
    timezone_name: str
    calendar_id: str
    calendar_version: str
    calendar_semantic_hash: str
    intervals: tuple[SessionInterval, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        parsed_trade_date = parse_trade_date(self.trade_date)
        if parsed_trade_date.isoformat() != self.trade_date:
            raise ValueError("trade_date must be strict YYYY-MM-DD")
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown session timezone") from exc
        if not self.calendar_id or not self.calendar_version or not self.calendar_semantic_hash:
            raise ValueError("calendar identity is required")
        intervals = tuple(self.intervals)
        if not intervals:
            raise ValueError("session plan requires intervals")
        if intervals != tuple(sorted(intervals, key=lambda item: item.start_ms_of_day)):
            raise ValueError("session intervals must be ordered")
        cursor = 0
        for interval in intervals:
            if interval.start_ms_of_day != cursor:
                raise ValueError("session intervals must cover the day without gaps or overlap")
            cursor = interval.end_exclusive_ms_of_day
        if cursor != MILLISECONDS_PER_DAY:
            raise ValueError("session intervals must cover the full local day")
        object.__setattr__(self, "intervals", intervals)
        object.__setattr__(
            self,
            "content_hash",
            semantic_hash(
                {
                    "contract_version": SESSION_PLAN_CONTRACT_VERSION,
                    "trade_date": self.trade_date,
                    "timezone": self.timezone_name,
                    "calendar_id": self.calendar_id,
                    "calendar_version": self.calendar_version,
                    "calendar_semantic_hash": self.calendar_semantic_hash,
                    "intervals": tuple(
                        {
                            "phase": item.phase,
                            "start_time": item.start_time,
                            "end_exclusive_time": item.end_exclusive_time,
                        }
                        for item in intervals
                    ),
                }
            ),
        )

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def phase_at(self, as_of: datetime) -> str:
        """Return the phase at an aware instant belonging to this trade date."""

        if not isinstance(as_of, datetime):
            raise TypeError("as_of must be a timezone-aware datetime")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        local = as_of.astimezone(self.timezone)
        if local.date().isoformat() != self.trade_date:
            raise ValueError("as_of local date does not match session trade_date")
        value = (
            ((local.hour * 60 + local.minute) * 60 + local.second) * 1000
            + local.microsecond // 1000
        )
        for interval in self.intervals:
            if interval.contains_ms_of_day(value):
                return interval.phase
        raise RuntimeError("session plan full-day coverage invariant failed")

    def phase_at_ms(self, epoch_ms: int) -> str:
        """Return the phase for an explicit epoch millisecond."""

        if isinstance(epoch_ms, bool) or not isinstance(epoch_ms, int):
            raise TypeError("epoch_ms must be an integer")
        if epoch_ms <= 0:
            raise ValueError("epoch_ms must be positive")
        seconds, milliseconds = divmod(epoch_ms, 1000)
        try:
            as_of = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
                microsecond=milliseconds * 1000
            )
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("epoch_ms is outside supported datetime range") from exc
        return self.phase_at(as_of)


def build_a_share_session_plan(
    trade_date: str,
    calendar: TradingCalendarSnapshot,
) -> SessionPlan:
    """Build the current A-share runtime phase plan for one trading day.

    The intervals preserve the meaningful legacy phase boundaries while
    applying the new system's universal half-open interval rule.  In
    particular, exactly 15:00 belongs to POSTMARKET rather than extending the
    afternoon interval by one unrepresentable instant.
    """

    parsed = parse_trade_date(trade_date)
    canonical = parsed.isoformat()
    if canonical != trade_date:
        raise ValueError("trade_date must be strict YYYY-MM-DD")
    if not calendar.declared_from <= parsed <= calendar.declared_to:
        raise CalendarCoverageError(
            "session trade date outside calendar declared coverage: " + canonical
        )
    if not calendar.is_trading_day(parsed):
        raise ValueError("session plan requires a trading day")
    intervals: Iterable[SessionInterval] = (
        SessionInterval("PREMARKET", "00:00:00", "09:15:00"),
        SessionInterval("AUCTION", "09:15:00", "09:30:00"),
        SessionInterval("INTRADAY", "09:30:00", "11:30:00"),
        SessionInterval("LUNCH_BREAK", "11:30:00", "13:00:00"),
        SessionInterval("INTRADAY", "13:00:00", "15:00:00"),
        SessionInterval("POSTMARKET", "15:00:00", "24:00:00"),
    )
    return SessionPlan(
        trade_date=canonical,
        timezone_name=calendar.timezone_name,
        calendar_id=calendar.calendar_id,
        calendar_version=calendar.version,
        calendar_semantic_hash=calendar.semantic_hash,
        intervals=tuple(intervals),
    )
