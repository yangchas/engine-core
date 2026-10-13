"""Pure derivation of a previous-day limit-up pool from TD daily klines."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class LimitUpPoolDerivation:
    rows: tuple[dict[str, Any], ...]
    input_symbol_count: int
    excluded_counts: Mapping[str, int]
    notes: Mapping[str, Any]
    content_hash: str


def _strict_date(value: str) -> date:
    if not isinstance(value, str):
        raise TypeError("trade_date must be strict YYYY-MM-DD text")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("trade_date must be strict YYYY-MM-DD text")
    return parsed


def _row_date(ts: Any) -> date | None:
    if isinstance(ts, datetime):
        return ts.date()
    if not isinstance(ts, str):
        return None
    try:
        return date.fromisoformat(ts[:10])
    except ValueError:
        return None


def _finite_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _limit_pct(symbol: str, st_symbols: frozenset[str]) -> float:
    if symbol in st_symbols and not symbol.startswith(("30", "688")):
        return 5.0
    if symbol.startswith(("688", "689", "30")):
        return 20.0
    if symbol.startswith(("8", "4", "92")):
        return 30.0
    return 10.0


def _is_limit_up(row: Sequence[Any], symbol: str, st_symbols: frozenset[str], tolerance_pct: float):
    if len(row) != 9:
        return False, True
    high, close, pct = (_finite_or_none(row[index]) for index in (2, 4, 8))
    if high is None or close is None or pct is None:
        return False, True
    return pct >= _limit_pct(symbol, st_symbols) - tolerance_pct and abs(close - high) < 1e-3, False


def _weekday_gap(previous: date, current: date) -> bool:
    """Detect omitted weekdays; weekends are not trading-session gaps."""
    day = previous + timedelta(days=1)
    while day < current:
        if day.weekday() < 5:
            return True
        day += timedelta(days=1)
    return False


def derive_limit_up_pool_from_daily_klines(
    klines_by_symbol: Mapping[str, Sequence[Sequence[Any]]],
    trade_date: str,
    *,
    names_by_symbol: Mapping[str, str] | None = None,
    plate_by_symbol: Mapping[str, str | None] | None = None,
    st_symbols: frozenset[str] = frozenset(),
    tolerance_pct: float = 0.6,
) -> LimitUpPoolDerivation:
    target = _strict_date(trade_date)
    tolerance = _finite_or_none(tolerance_pct)
    if tolerance is None or tolerance < 0:
        raise ValueError("tolerance_pct must be a finite nonnegative number")
    if not isinstance(st_symbols, (set, frozenset)):
        raise TypeError("st_symbols must be a set or frozenset")
    names_by_symbol = names_by_symbol or {}
    plate_by_symbol = plate_by_symbol or {}
    excluded = {"no_row_for_trade_date": 0, "unknown_pct_or_price": 0, "not_limit_up": 0}
    name_fallbacks = 0
    output: list[dict[str, Any]] = []

    for symbol, supplied_rows in klines_by_symbol.items():
        dated: list[tuple[date, Sequence[Any]]] = []
        for row in supplied_rows:
            if len(row) == 9:
                row_day = _row_date(row[0])
                if row_day is not None:
                    dated.append((row_day, row))
        dated.sort(key=lambda item: item[0])
        current = next((i for i in range(len(dated) - 1, -1, -1) if dated[i][0] == target), None)
        if current is None:
            excluded["no_row_for_trade_date"] += 1
            continue
        is_up, unknown = _is_limit_up(dated[current][1], symbol, st_symbols, tolerance)
        if unknown:
            excluded["unknown_pct_or_price"] += 1
            continue
        if not is_up:
            excluded["not_limit_up"] += 1
            continue

        lb_days = 1
        cursor = current - 1
        next_day = target
        while cursor >= 0:
            previous_day, previous_row = dated[cursor]
            if previous_day >= next_day or _weekday_gap(previous_day, next_day):
                break
            prior_up, prior_unknown = _is_limit_up(previous_row, symbol, st_symbols, tolerance)
            if prior_unknown or not prior_up:
                break
            lb_days += 1
            next_day = previous_day
            cursor -= 1

        name = names_by_symbol.get(symbol)
        if not isinstance(name, str) or not name:
            name = symbol
            name_fallbacks += 1
        row = dated[current][1]
        output.append({
            "trade_date": trade_date,
            "symbol": symbol,
            "name": name,
            "lb_days": lb_days,
            "plate": plate_by_symbol.get(symbol),
            "seal_time": None,
            "turnover": _finite_or_none(row[6]),
            "close_pct": _finite_or_none(row[8]),
            "source": "core_td_kline",
        })

    output.sort(key=lambda item: item["symbol"])
    payload = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return LimitUpPoolDerivation(
        rows=tuple(output),
        input_symbol_count=len(klines_by_symbol),
        excluded_counts=excluded,
        notes={"name_fallback_symbols": name_fallbacks},
        content_hash=digest,
    )
