"""Adapt raw 0925 auction rows to the previous-day limit feedback contract.

``build_previous_day_limit_feedback`` expects already-normalized *current*
rows (``symbol``, ``trade_date``, ``tag='0925'``, percentage-point
``change_pct``, ``auction_amount_yuan``, ``source_record_time_ms``).  This
module is the pure, source-independent adapter for TD-shaped auction rows
(``auction_tag``/``chg_bp``/``match_amt_yuan``/``ts``).  It performs no I/O.

Rules: a missing or invalid value stays ``None`` (never zero); rows for another
tag or another trade date are counted and skipped, not repaired; duplicate
symbols keep the row with the latest source time and are reported.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

from .auction import normalize_auction_change_bp_to_pct
from .contracts import canonical_hash
from .q2 import normalize_symbol

CURRENT_ROWS_CONTRACT_VERSION = "PreviousDayLimitCurrentRows0925V1"
_TAG = "0925"
_TD_COLUMNS = (
    "ts", "px_milli", "chg_bp", "match_amt_yuan", "rest_bid_amt_yuan",
    "rest_ask_amt_yuan", "limit_state", "symbol", "trade_date", "auction_tag",
)


@dataclass(frozen=True)
class Current0925Rows:
    """Normalized current rows plus counts of everything not carried over."""

    trade_date: str
    rows: Tuple[Mapping[str, Any], ...]
    input_row_count: int
    other_tag_count: int
    invalid_row_count: int
    date_mismatch_count: int
    duplicate_symbols: Tuple[str, ...]
    content_hash: str

    def as_trace(self) -> Mapping[str, Any]:
        return {
            "contract": CURRENT_ROWS_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "row_count": len(self.rows),
            "input_row_count": self.input_row_count,
            "other_tag_count": self.other_tag_count,
            "invalid_row_count": self.invalid_row_count,
            "date_mismatch_count": self.date_mismatch_count,
            "duplicate_symbols": self.duplicate_symbols,
            "content_hash": self.content_hash,
        }


def _dashed_date(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        text = "%s-%s-%s" % (text[:4], text[4:6], text[6:8])
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None
    return parsed.isoformat() if parsed.isoformat() == text else None


def _epoch_ms(value: Any, timezone_name: str) -> Optional[int]:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    aware = value
    if aware.tzinfo is None or aware.utcoffset() is None:
        aware = aware.replace(tzinfo=ZoneInfo(timezone_name))
    return int(aware.astimezone(timezone.utc).timestamp() * 1000)


def _amount_yuan(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or value < 0:
        return None
    return int(round(float(value)))


def _as_mapping(raw: Any) -> Optional[Mapping[str, Any]]:
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, (list, tuple)) and len(raw) == len(_TD_COLUMNS):
        return dict(zip(_TD_COLUMNS, raw))
    return None


def build_current_0925_rows(
    trade_date: str,
    td_rows: Iterable[Any],
    *,
    timezone_name: str = "Asia/Shanghai",
) -> Current0925Rows:
    """Return feedback-contract rows for ``trade_date`` from TD auction rows."""

    requested = _dashed_date(trade_date)
    if requested is None:
        raise ValueError("trade_date must be strict YYYY-MM-DD")
    by_symbol: dict = {}
    duplicates: set = set()
    total = other_tag = invalid = mismatch = 0
    for raw in td_rows:
        total += 1
        row = _as_mapping(raw)
        if row is None:
            invalid += 1
            continue
        if str(row.get("auction_tag") or row.get("tag") or "") != _TAG:
            other_tag += 1
            continue
        row_date = _dashed_date(row.get("trade_date"))
        if row_date is None:
            invalid += 1
            continue
        if row_date != requested:
            mismatch += 1
            continue
        try:
            symbol = normalize_symbol(str(row.get("symbol", "")))
        except (TypeError, ValueError):
            invalid += 1
            continue
        record = {
            "symbol": symbol,
            "trade_date": requested,
            "tag": _TAG,
            "change_pct": normalize_auction_change_bp_to_pct(row.get("chg_bp")),
            "auction_amount_yuan": _amount_yuan(
                row.get("match_amt_yuan", row.get("auction_amount_yuan"))
            ),
            "source_record_time_ms": _epoch_ms(
                row.get("ts") or row.get("timestamp"), timezone_name
            ),
        }
        previous = by_symbol.get(symbol)
        if previous is not None:
            duplicates.add(symbol)
            old_time = previous["source_record_time_ms"]
            new_time = record["source_record_time_ms"]
            if (new_time or -1) < (old_time or -1):
                continue
        by_symbol[symbol] = record
    rows = tuple(by_symbol[symbol] for symbol in sorted(by_symbol))
    return Current0925Rows(
        trade_date=requested,
        rows=rows,
        input_row_count=total,
        other_tag_count=other_tag,
        invalid_row_count=invalid,
        date_mismatch_count=mismatch,
        duplicate_symbols=tuple(sorted(duplicates)),
        content_hash=canonical_hash({"trade_date": requested, "rows": rows}),
    )


__all__ = [
    "CURRENT_ROWS_CONTRACT_VERSION",
    "Current0925Rows",
    "build_current_0925_rows",
]
