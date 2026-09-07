"""Versioned trading-day primitives used by date-sensitive data functions.

The snapshot is the runtime authority for the dates it covers.  It is built
offline from an observed source (currently BaoStock on the formal server) and
contains no network or fallback behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import evidence_hash, semantic_hash


CALENDAR_CONTRACT_VERSION = "TradingCalendarSnapshotV1"
DEFAULT_CALENDAR_ID = "CN_A_SHARE"
DEFAULT_CALENDAR_TIMEZONE = "Asia/Shanghai"


class CalendarCoverageError(ValueError):
    """Raised when a date is outside the snapshot's declared/guard coverage."""


class CalendarSnapshotError(ValueError):
    """Raised when a snapshot violates its immutable calendar contract."""


def parse_trade_date(value: date | str) -> date:
    """Parse only a date or strict ``YYYY-MM-DD`` string.

    ``datetime`` is deliberately rejected even though it subclasses ``date``;
    callers must not silently discard a time or timezone component.
    """

    if isinstance(value, datetime):
        raise TypeError("datetime is not accepted as a trade date")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        import re

        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
            raise ValueError("trade date must be strict YYYY-MM-DD")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("trade date must be a valid YYYY-MM-DD") from exc
    raise TypeError("trade date must be date or strict YYYY-MM-DD string")


def _date_text(value: date) -> str:
    return value.isoformat()


@dataclass(frozen=True)
class TradingCalendarSnapshot:
    """Immutable, versioned trading-date evidence and runtime authority."""

    calendar_id: str
    version: str
    timezone_name: str
    declared_valid_from: str
    declared_valid_to: str
    source_guard_valid_from: str
    source_guard_valid_to: str
    trading_dates: tuple[str, ...]
    source_id: Optional[str] = None
    observed_at_ms: Optional[int] = None
    evidence_ref: Optional[str] = None
    semantic_hash: str = field(init=False)
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.calendar_id or not self.version:
            raise CalendarSnapshotError("calendar_id and version are required")
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise CalendarSnapshotError("unknown calendar timezone") from exc
        declared_from = parse_trade_date(self.declared_valid_from)
        declared_to = parse_trade_date(self.declared_valid_to)
        guard_from = parse_trade_date(self.source_guard_valid_from)
        guard_to = parse_trade_date(self.source_guard_valid_to)
        if declared_from > declared_to or guard_from > guard_to:
            raise CalendarSnapshotError("calendar coverage bounds are inverted")
        if guard_from > declared_from or guard_to < declared_to:
            raise CalendarSnapshotError("source guard coverage must contain declared coverage")
        canonical_dates = tuple(self.trading_dates)
        if canonical_dates != tuple(sorted(set(canonical_dates))):
            raise CalendarSnapshotError("trading_dates must be sorted and unique")
        for value in canonical_dates:
            parsed = parse_trade_date(value)
            if _date_text(parsed) != value:
                raise CalendarSnapshotError("trading_dates must use YYYY-MM-DD")
            if not guard_from <= parsed <= guard_to:
                raise CalendarSnapshotError("trading date is outside source guard coverage")
        if self.observed_at_ms is not None and self.observed_at_ms <= 0:
            raise CalendarSnapshotError("observed_at_ms must be positive")
        object.__setattr__(self, "trading_dates", canonical_dates)
        semantic_payload = {
            "contract_version": CALENDAR_CONTRACT_VERSION,
            "calendar_id": self.calendar_id,
            "version": self.version,
            "timezone": self.timezone_name,
            "declared_valid_from": _date_text(declared_from),
            "declared_valid_to": _date_text(declared_to),
            "source_guard_valid_from": _date_text(guard_from),
            "source_guard_valid_to": _date_text(guard_to),
            "trading_dates": canonical_dates,
        }
        object.__setattr__(self, "semantic_hash", semantic_hash(semantic_payload))
        evidence_payload = {
            "contract_version": CALENDAR_CONTRACT_VERSION,
            "calendar_semantic_hash": self.semantic_hash,
            "source_id": self.source_id,
            "observed_at_ms": self.observed_at_ms,
            "evidence_ref": self.evidence_ref,
        }
        object.__setattr__(self, "evidence_hash", evidence_hash(evidence_payload))

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @property
    def declared_from(self) -> date:
        return parse_trade_date(self.declared_valid_from)

    @property
    def declared_to(self) -> date:
        return parse_trade_date(self.declared_valid_to)

    @property
    def guard_from(self) -> date:
        return parse_trade_date(self.source_guard_valid_from)

    @property
    def guard_to(self) -> date:
        return parse_trade_date(self.source_guard_valid_to)

    def _require_declared(self, value: date | str) -> date:
        parsed = parse_trade_date(value)
        if not self.declared_from <= parsed <= self.declared_to:
            raise CalendarCoverageError(
                "trade date outside declared coverage: " + parsed.isoformat()
            )
        return parsed

    def is_trading_day(self, value: date | str) -> bool:
        parsed = parse_trade_date(value)
        if not self.guard_from <= parsed <= self.guard_to:
            raise CalendarCoverageError(
                "trade date outside source guard coverage: " + parsed.isoformat()
            )
        return parsed.isoformat() in self.trading_dates

    def previous_trade_day(self, value: date | str) -> date:
        parsed = self._require_declared(value)
        candidates = [
            parse_trade_date(item)
            for item in self.trading_dates
            if self.guard_from <= parse_trade_date(item) < parsed
        ]
        if not candidates:
            raise CalendarCoverageError("no previous trade day in source guard coverage")
        return max(candidates)

    def next_trade_day(self, value: date | str) -> date:
        parsed = self._require_declared(value)
        candidates = [
            parse_trade_date(item)
            for item in self.trading_dates
            if parsed < parse_trade_date(item) <= self.guard_to
        ]
        if not candidates:
            raise CalendarCoverageError("no next trade day in source guard coverage")
        return min(candidates)

    def latest_completed_trade_day(
        self,
        as_of: datetime,
        *,
        completion_cutoff_time: time = time(15, 30),
    ) -> date:
        """Return the latest date completed under caller-supplied policy.

        ``completion_cutoff_time`` describes data readiness, not a calendar
        source fact.  Equality at the cutoff is considered completed.
        """

        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        if completion_cutoff_time.tzinfo is not None:
            raise ValueError("completion_cutoff_time must be timezone-naive")
        local = as_of.astimezone(self.timezone)
        local_date = self._require_declared(local.date())
        if self.is_trading_day(local_date) and local.time() >= completion_cutoff_time:
            return local_date
        return self.previous_trade_day(local_date)


def validate_calendar_version_identity(
    existing: TradingCalendarSnapshot,
    candidate: TradingCalendarSnapshot,
) -> None:
    """Reject reuse of one calendar version for different semantic content."""

    if (
        existing.calendar_id == candidate.calendar_id
        and existing.version == candidate.version
        and existing.semantic_hash != candidate.semantic_hash
    ):
        raise CalendarSnapshotError(
            "same calendar_id/version has different semantic hash"
        )


def build_calendar_snapshot(
    trading_dates: Iterable[date | str],
    *,
    version: str,
    declared_valid_from: date | str,
    declared_valid_to: date | str,
    source_guard_valid_from: date | str,
    source_guard_valid_to: date | str,
    calendar_id: str = DEFAULT_CALENDAR_ID,
    timezone_name: str = DEFAULT_CALENDAR_TIMEZONE,
    source_id: Optional[str] = None,
    observed_at_ms: Optional[int] = None,
    evidence_ref: Optional[str] = None,
) -> TradingCalendarSnapshot:
    """Create a canonical snapshot from date values supplied by an offline probe."""

    canonical_dates = tuple(sorted({_date_text(parse_trade_date(item)) for item in trading_dates}))
    return TradingCalendarSnapshot(
        calendar_id=calendar_id,
        version=version,
        timezone_name=timezone_name,
        declared_valid_from=_date_text(parse_trade_date(declared_valid_from)),
        declared_valid_to=_date_text(parse_trade_date(declared_valid_to)),
        source_guard_valid_from=_date_text(parse_trade_date(source_guard_valid_from)),
        source_guard_valid_to=_date_text(parse_trade_date(source_guard_valid_to)),
        trading_dates=canonical_dates,
        source_id=source_id,
        observed_at_ms=observed_at_ms,
        evidence_ref=evidence_ref,
    )
