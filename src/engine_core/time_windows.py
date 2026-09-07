"""Deterministic minute-window metrics for normalized market observations.

This module is a small, stateful wheel extracted from the legacy
``TickWindowTracker`` behavior.  It deliberately requires an epoch source
timestamp and an explicit integer unit contract; it never falls back to the
machine clock, strips qualified symbols, or applies strategy thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Dict, Mapping, Optional
from zoneinfo import ZoneInfo

from .calendar import parse_trade_date
from .contracts import semantic_hash, trunc_div
from .q2 import normalize_symbol


SHANGHAI = ZoneInfo("Asia/Shanghai")
MINUTES_PER_DAY = 24 * 60


def minute_key_from_timestamp_ms(
    source_time_ms: int,
    *,
    timezone_name: str = "Asia/Shanghai",
) -> tuple[str, int]:
    """Return ``(local_trade_date, minute_of_day)`` for an epoch timestamp.

    ``source_time_ms`` is an observed source timestamp, not a wall-clock
    fallback.  The timezone is explicit so Windows and Linux derive the same
    bucket.
    """

    if isinstance(source_time_ms, bool) or not isinstance(source_time_ms, int):
        raise TypeError("source_time_ms must be an integer epoch millisecond")
    if source_time_ms <= 0:
        raise ValueError("source_time_ms must be positive")
    try:
        seconds, milliseconds = divmod(source_time_ms, 1000)
        local = datetime.fromtimestamp(
            seconds,
            tz=ZoneInfo(timezone_name),
        ).replace(microsecond=milliseconds * 1000)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("source_time_ms is outside supported datetime range") from exc
    minute = local.hour * 60 + local.minute
    if not 0 <= minute < MINUTES_PER_DAY:
        raise ValueError("derived minute index is outside one day")
    return local.date().isoformat(), minute


@dataclass(frozen=True)
class MinuteObservation:
    """One canonical cumulative observation retained in a minute bucket."""

    trade_date: str
    minute_index: int
    source_time_ms: int
    symbol: str
    price_milli: int
    amount_yuan: int


@dataclass(frozen=True)
class MinuteWindowMetrics:
    """Derived metrics for one symbol and one local minute."""

    trade_date: str
    symbol: str
    minute_index: int
    source_time_ms: int
    price_milli: int
    amount_yuan: int
    price_change_bp: Optional[int]
    price_change_reason: str
    amount_2m_yuan: Optional[int]
    amount_2m_reason: str

    @property
    def content_hash(self) -> str:
        return semantic_hash(
            {
                "trade_date": self.trade_date,
                "symbol": self.symbol,
                "minute_index": self.minute_index,
                "source_time_ms": self.source_time_ms,
                "price_milli": self.price_milli,
                "amount_yuan": self.amount_yuan,
                "price_change_bp": self.price_change_bp,
                "price_change_reason": self.price_change_reason,
                "amount_2m_yuan": self.amount_2m_yuan,
                "amount_2m_reason": self.amount_2m_reason,
            }
        )


class MinuteWindowTracker:
    """Keep a bounded per-symbol minute history and derive neutral metrics.

    ``amount_yuan`` is expected to be a non-decreasing cumulative amount for
    the session.  A decrease is exposed as ``COUNTER_RESET`` and never
    converted into a fabricated positive delta.  The amount metric follows
    the legacy bounded lookback: it uses the oldest available point within
    the preceding two minute slots, while the one-minute price metric requires
    the immediately preceding slot.
    """

    def __init__(self, *, keep_minutes: int = 5, timezone_name: str = "Asia/Shanghai") -> None:
        if isinstance(keep_minutes, bool) or not isinstance(keep_minutes, int):
            raise TypeError("keep_minutes must be an integer")
        if keep_minutes < 1:
            raise ValueError("keep_minutes must be positive")
        try:
            ZoneInfo(timezone_name)
        except Exception as exc:
            raise ValueError("unknown tracker timezone") from exc
        self.keep_minutes = keep_minutes
        self.timezone_name = timezone_name
        self._history: Dict[str, Dict[str, Dict[int, MinuteObservation]]] = {}

    def observe(
        self,
        symbol: object,
        *,
        price_milli: int,
        amount_yuan: int,
        source_time_ms: int,
    ) -> MinuteWindowMetrics:
        """Insert one observation and return metrics for its minute bucket."""

        normalized_symbol = normalize_symbol(symbol)
        if isinstance(price_milli, bool) or not isinstance(price_milli, int):
            raise TypeError("price_milli must be an integer")
        if price_milli <= 0:
            raise ValueError("price_milli must be positive")
        if isinstance(amount_yuan, bool) or not isinstance(amount_yuan, int):
            raise TypeError("amount_yuan must be an integer")
        if amount_yuan < 0:
            raise ValueError("amount_yuan must be non-negative")
        trade_date, minute_index = minute_key_from_timestamp_ms(
            source_time_ms,
            timezone_name=self.timezone_name,
        )
        observation = MinuteObservation(
            trade_date=trade_date,
            minute_index=minute_index,
            source_time_ms=source_time_ms,
            symbol=normalized_symbol,
            price_milli=price_milli,
            amount_yuan=amount_yuan,
        )
        by_date = self._history.setdefault(trade_date, {})
        by_minute = by_date.setdefault(normalized_symbol, {})
        existing = by_minute.get(minute_index)
        if existing is not None:
            if source_time_ms < existing.source_time_ms:
                return self._metrics_after_observe(
                    normalized_symbol,
                    trade_date=trade_date,
                    minute_index=minute_index,
                )
            if source_time_ms == existing.source_time_ms:
                if observation != existing:
                    raise ValueError("same source timestamp has conflicting observations")
                return self._metrics_after_observe(
                    normalized_symbol,
                    trade_date=trade_date,
                    minute_index=minute_index,
                )
        by_minute[minute_index] = observation
        self._trim(by_minute)
        return self._metrics_after_observe(
            normalized_symbol,
            trade_date=trade_date,
            minute_index=minute_index,
        )

    def _metrics_after_observe(
        self,
        symbol: str,
        *,
        trade_date: str,
        minute_index: int,
    ) -> MinuteWindowMetrics:
        metrics = self.get_metrics(
            symbol,
            trade_date=trade_date,
            minute_index=minute_index,
        )
        if metrics is None:  # pragma: no cover - protects an internal invariant
            raise RuntimeError("observed minute was not retained")
        return metrics

    def get_metrics(
        self,
        symbol: object,
        *,
        trade_date: str,
        minute_index: Optional[int] = None,
    ) -> Optional[MinuteWindowMetrics]:
        """Read metrics without mutating the tracker."""

        normalized_symbol = normalize_symbol(symbol)
        if not isinstance(trade_date, str):
            raise TypeError("trade_date must be strict YYYY-MM-DD text")
        canonical_trade_date = parse_trade_date(trade_date).isoformat()
        if canonical_trade_date != trade_date:
            raise ValueError("trade_date must be strict YYYY-MM-DD")
        by_minute = self._history.get(canonical_trade_date, {}).get(normalized_symbol)
        if not by_minute:
            return None
        minute = max(by_minute) if minute_index is None else minute_index
        if isinstance(minute, bool) or not isinstance(minute, int):
            raise TypeError("minute_index must be an integer")
        if not 0 <= minute < MINUTES_PER_DAY:
            raise ValueError("minute_index must be between 0 and 1439")
        current = by_minute.get(minute)
        if current is None:
            return None
        previous = by_minute.get(minute - 1)
        if previous is None:
            price_change_bp = None
            price_change_reason = "MISSING_REFERENCE"
        else:
            price_change_bp = trunc_div(
                (current.price_milli - previous.price_milli) * 10_000,
                previous.price_milli,
            )
            price_change_reason = "READY"

        two_minute_reference = by_minute.get(minute - 2) or by_minute.get(minute - 1)
        if two_minute_reference is None:
            amount_2m_yuan = None
            amount_2m_reason = "MISSING_REFERENCE"
        elif current.amount_yuan < two_minute_reference.amount_yuan:
            amount_2m_yuan = None
            amount_2m_reason = "COUNTER_RESET"
        else:
            amount_2m_yuan = current.amount_yuan - two_minute_reference.amount_yuan
            amount_2m_reason = "READY"
        return MinuteWindowMetrics(
            trade_date=trade_date,
            symbol=normalized_symbol,
            minute_index=minute,
            source_time_ms=current.source_time_ms,
            price_milli=current.price_milli,
            amount_yuan=current.amount_yuan,
            price_change_bp=price_change_bp,
            price_change_reason=price_change_reason,
            amount_2m_yuan=amount_2m_yuan,
            amount_2m_reason=amount_2m_reason,
        )

    def get_all_metrics(
        self,
        *,
        trade_date: str,
        minute_index: Optional[int] = None,
    ) -> Mapping[str, MinuteWindowMetrics]:
        """Return sorted immutable-by-convention metrics for one date."""

        result: Dict[str, MinuteWindowMetrics] = {}
        if not isinstance(trade_date, str):
            raise TypeError("trade_date must be strict YYYY-MM-DD text")
        canonical_trade_date = parse_trade_date(trade_date).isoformat()
        if canonical_trade_date != trade_date:
            raise ValueError("trade_date must be strict YYYY-MM-DD")
        for symbol in sorted(self._history.get(canonical_trade_date, {})):
            metric = self.get_metrics(symbol, trade_date=canonical_trade_date, minute_index=minute_index)
            if metric is not None:
                result[symbol] = metric
        return MappingProxyType(result)

    def _trim(self, by_minute: Dict[int, MinuteObservation]) -> None:
        latest = max(by_minute)
        lower_bound = latest - self.keep_minutes + 1
        for minute in tuple(by_minute):
            if minute < lower_bound:
                by_minute.pop(minute, None)
