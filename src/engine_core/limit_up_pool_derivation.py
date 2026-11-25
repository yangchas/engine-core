"""Pure derivation of a previous-day limit-up pool from TD daily klines.

ST and ordinary stocks currently share the same limit percentages: TD data
from 2026-06-26 through 2026-10-12 showed 125 ST symbols reaching roughly
10% daily gains and losses. Earlier dates have not been verified; streak
counts reaching before 2026-06-26 may be affected by historical 5% rules and
must not be treated as historically correct.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Collection, Mapping, Optional, Sequence


@dataclass(frozen=True)
class LimitUpPoolDerivation:
    rows: tuple[dict[str, Any], ...]
    input_symbol_count: int
    excluded_counts: Mapping[str, int]
    notes: Mapping[str, Any]
    content_hash: str
    seal_evidence_mode: str


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


def _limit_pct(symbol: str) -> float:
    if symbol.startswith(("688", "689", "30")):
        return 20.0
    if symbol.startswith(("8", "4", "92")):
        return 30.0
    return 10.0


_CENT = Decimal("0.01")
_PCT_TOLERANCE = Decimal("0.05")


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _round_cent(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _previous_session_row(
    dated_rows: Mapping[date, Sequence[Any]], day: date, ordered_sessions: Sequence[date]
) -> Sequence[Any] | None:
    for previous_day in reversed(ordered_sessions):
        if previous_day < day:
            return dated_rows.get(previous_day)
    return None


def _pre_close(
    row: Sequence[Any], previous_row: Sequence[Any] | None
) -> tuple[Decimal | None, str | None]:
    close = _decimal_or_none(row[4])
    pct = _decimal_or_none(row[8])
    if close is None:
        return None, None
    close = _round_cent(close)

    previous_close = _decimal_or_none(previous_row[4]) if previous_row is not None else None
    if previous_close is not None:
        previous_close = _round_cent(previous_close)
        if pct is not None:
            if previous_close > 0:
                observed_pct = (close / previous_close - Decimal(1)) * Decimal(100)
            else:
                observed_pct = None
            if observed_pct is None or abs(observed_pct - pct) > _PCT_TOLERANCE:
                denominator = Decimal(1) + pct / Decimal(100)
                if denominator <= 0:
                    return None, None
                return _round_cent(close / denominator), "FROM_PCT_CHG"
        return previous_close, "PREV_CLOSE"

    if pct is None:
        return None, None
    denominator = Decimal(1) + pct / Decimal(100)
    if denominator <= 0:
        return None, None
    return _round_cent(close / denominator), "FROM_PCT_CHG"


def _is_limit_up(
    row: Sequence[Any], symbol: str, previous_row: Sequence[Any] | None
) -> tuple[bool, bool, str | None]:
    if len(row) != 9:
        return False, True, None
    close = _decimal_or_none(row[4])
    if close is None:
        return False, True, None
    close = _round_cent(close)
    pre_close, basis = _pre_close(row, previous_row)
    if pre_close is None:
        return False, False, None
    limit_up_price = _round_cent(
        pre_close * (Decimal(1) + Decimal(str(_limit_pct(symbol))) / Decimal(100))
    )
    return close == limit_up_price and close > pre_close, False, basis


def derive_limit_up_pool_from_daily_klines(
    klines_by_symbol: Mapping[str, Sequence[Sequence[Any]]],
    trade_date: str,
    *,
    names_by_symbol: Mapping[str, str] | None = None,
    plate_by_symbol: Mapping[str, str | None] | None = None,
    trading_dates: Collection[str] | None = None,
    close_limit_state_by_symbol: Optional[Mapping[str, int]] = None,
    st_symbols: Collection[str] | None = None,
    exclude_st: bool = True,
    st_source: str | None = None,
) -> LimitUpPoolDerivation:
    target = _strict_date(trade_date)
    names_by_symbol = names_by_symbol or {}
    plate_by_symbol = plate_by_symbol or {}
    rows_by_symbol: dict[str, dict[date, Sequence[Any]]] = {}
    symbols_by_date: dict[date, set[str]] = {}
    for symbol, supplied_rows in klines_by_symbol.items():
        dated_rows: dict[date, Sequence[Any]] = {}
        for row in supplied_rows:
            if len(row) != 9:
                continue
            row_day = _row_date(row[0])
            if row_day is None:
                continue
            dated_rows[row_day] = row
            symbols_by_date.setdefault(row_day, set()).add(symbol)
        rows_by_symbol[symbol] = dated_rows

    if trading_dates is None:
        symbol_count = len(klines_by_symbol)
        session_dates = {
            row_day for row_day, symbols in symbols_by_date.items()
            if symbol_count and 2 * len(symbols) >= symbol_count
        }
    else:
        session_dates = {_strict_date(value) for value in trading_dates}
    ordered_sessions = sorted(session_dates)
    trade_date_in_sessions = target in session_dates

    excluded = {
        "no_row_for_trade_date": 0, "unknown_pct_or_price": 0,
        "no_pre_close": 0, "not_limit_up": 0, "limit_price_without_seal": 0,
        "st": 0,
    }
    st_symbol_set = set(st_symbols) if st_symbols is not None else None
    name_fallbacks = 0
    no_seal_symbols: list[str] = []
    output: list[dict[str, Any]] = []

    for symbol, supplied_rows in klines_by_symbol.items():
        dated = rows_by_symbol[symbol]
        current_row = dated.get(target)
        if current_row is None:
            excluded["no_row_for_trade_date"] += 1
            continue
        current_previous_row = _previous_session_row(dated, target, ordered_sessions)
        is_up, unknown, pre_close_basis = _is_limit_up(
            current_row, symbol, current_previous_row
        )
        if unknown:
            excluded["unknown_pct_or_price"] += 1
            continue
        if pre_close_basis is None:
            excluded["no_pre_close"] += 1
            continue
        if not is_up:
            excluded["not_limit_up"] += 1
            continue
        if exclude_st and st_symbol_set is not None and symbol in st_symbol_set:
            excluded["st"] += 1
            continue
        if close_limit_state_by_symbol is not None and symbol in close_limit_state_by_symbol:
            if close_limit_state_by_symbol[symbol] != 1:
                excluded["limit_price_without_seal"] += 1
                no_seal_symbols.append(symbol)
                continue
            seal_basis = "Q2_LS_AT_CLOSE"
            lb_days_seal_basis = "TODAY_Q2_LS_HISTORY_KLINE"
        else:
            seal_basis = "KLINE_CLOSE_AT_LIMIT_NO_SEAL_EVIDENCE"
            lb_days_seal_basis = "KLINE_ONLY"

        lb_days = 1
        if trade_date_in_sessions:
            prior_sessions = (day for day in reversed(ordered_sessions) if day < target)
        else:
            prior_sessions = ()
        for previous_day in prior_sessions:
            previous_row = dated.get(previous_day)
            if previous_row is None:
                break
            prior_previous_row = _previous_session_row(dated, previous_day, ordered_sessions)
            prior_up, prior_unknown, _ = _is_limit_up(
                previous_row, symbol, prior_previous_row
            )
            if prior_unknown or not prior_up:
                break
            lb_days += 1

        name = names_by_symbol.get(symbol)
        if not isinstance(name, str) or not name:
            name = symbol
            name_fallbacks += 1
        row = current_row
        output.append({
            "trade_date": trade_date,
            "symbol": symbol,
            "name": name,
            "lb_days": lb_days,
            "plate": plate_by_symbol.get(symbol),
            "seal_time": None,
            "turnover": _finite_or_none(row[6]),
            "close_pct": _finite_or_none(row[8]),
            "pre_close_basis": pre_close_basis,
            "source": "core_td_kline",
            "seal_basis": seal_basis,
            "lb_days_seal_basis": lb_days_seal_basis,
        })

    output.sort(key=lambda item: item["symbol"])
    payload = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    notes = {"name_fallback_symbols": name_fallbacks}
    if exclude_st and st_symbol_set is None:
        notes["st_filter"] = "UNKNOWN_ST_LIST"
    else:
        notes["st_filter"] = "EXCLUDED" if exclude_st else "INCLUDED"
    notes["st_symbol_count"] = len(st_symbol_set) if st_symbol_set is not None else 0
    if st_source is not None:
        notes["st_source"] = st_source
    if no_seal_symbols:
        notes["limit_price_without_seal_symbols"] = sorted(no_seal_symbols)
    if not trade_date_in_sessions:
        notes["trade_date_not_in_trading_dates"] = True
    return LimitUpPoolDerivation(
        rows=tuple(output),
        input_symbol_count=len(klines_by_symbol),
        excluded_counts=excluded,
        notes=notes,
        content_hash=digest,
        seal_evidence_mode="Q2_LS" if close_limit_state_by_symbol is not None else "NONE",
    )
