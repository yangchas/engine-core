"""Pure normalizers for the per-symbol daily Redis caches.

``cache:hot_rank:{date}`` (THS attention rank) and ``cache:limit_truth:{date}``
(final limit-up truth) are hashes of symbol -> JSON row.  This module only
validates and shapes already-read rows; it performs no I/O.  A row that cannot
be trusted is counted and skipped, a missing optional field stays ``None``
(never zero), and duplicate symbols are reported rather than merged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Tuple

from .contracts import canonical_hash
from .previous_day_limit_current_rows import _dashed_date
from .q2 import normalize_symbol

SYMBOL_CACHE_CONTRACT_VERSION = "SymbolCacheRowsV1"
_DATASETS = {
    "hot_rank": ("rank", "heat", "name"),
    "limit_truth": ("lb_days",),
}


@dataclass(frozen=True)
class SymbolCacheRows:
    dataset: str
    trade_date: str
    rows: Tuple[Mapping[str, Any], ...]
    input_row_count: int
    invalid_row_count: int
    date_mismatch_count: int
    duplicate_symbols: Tuple[str, ...]
    missing_field_counts: Mapping[str, int]
    status: str
    content_hash: str

    def as_trace(self) -> Mapping[str, Any]:
        return {
            "contract": SYMBOL_CACHE_CONTRACT_VERSION,
            "dataset": self.dataset,
            "trade_date": self.trade_date,
            "row_count": len(self.rows),
            "input_row_count": self.input_row_count,
            "invalid_row_count": self.invalid_row_count,
            "date_mismatch_count": self.date_mismatch_count,
            "duplicate_symbols": self.duplicate_symbols,
            "missing_field_counts": dict(self.missing_field_counts),
            "status": self.status,
            "content_hash": self.content_hash,
        }


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) >= 1:
        return int(value)
    return None


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(float(value)) else None


def normalize_symbol_cache_rows(
    dataset: str, trade_date: str, rows: Iterable[Any]
) -> SymbolCacheRows:
    if dataset not in _DATASETS:
        raise ValueError("unsupported dataset: %s" % dataset)
    requested = _dashed_date(trade_date)
    if requested is None:
        raise ValueError("trade_date must be strict YYYY-MM-DD")
    by_symbol: dict = {}
    duplicates: set = set()
    total = invalid = mismatch = 0
    missing = {field: 0 for field in _DATASETS[dataset]}
    for raw in rows:
        total += 1
        if not isinstance(raw, Mapping):
            invalid += 1
            continue
        row_date = _dashed_date(raw.get("trade_date"))
        if row_date is not None and row_date != requested:
            mismatch += 1
            continue
        try:
            symbol = normalize_symbol(str(raw.get("symbol", "")))
        except (TypeError, ValueError):
            invalid += 1
            continue
        record: dict = {"symbol": symbol, "trade_date": requested,
                        "source": str(raw.get("source") or "") or None}
        if dataset == "hot_rank":
            record["rank"] = _positive_int(raw.get("rank"))
            record["heat"] = _finite(raw.get("heat"))
            record["name"] = str(raw.get("name") or "") or None
            if record["rank"] is None:
                invalid += 1  # a rank row without a rank carries no information
                continue
        else:
            record["lb_days"] = _positive_int(raw.get("lb_days"))
        for field in _DATASETS[dataset]:
            if record.get(field) is None:
                missing[field] += 1
        if symbol in by_symbol:
            duplicates.add(symbol)
        by_symbol[symbol] = record
    out = tuple(by_symbol[s] for s in sorted(by_symbol))
    if not out:
        status = "MISSING"
    elif invalid or mismatch or duplicates or any(missing.values()):
        status = "PARTIAL"
    else:
        status = "READY"
    return SymbolCacheRows(
        dataset=dataset, trade_date=requested, rows=out, input_row_count=total,
        invalid_row_count=invalid, date_mismatch_count=mismatch,
        duplicate_symbols=tuple(sorted(duplicates)), missing_field_counts=missing,
        status=status,
        content_hash=canonical_hash({"dataset": dataset, "trade_date": requested, "rows": out}),
    )


__all__ = ["SYMBOL_CACHE_CONTRACT_VERSION", "SymbolCacheRows", "normalize_symbol_cache_rows"]
