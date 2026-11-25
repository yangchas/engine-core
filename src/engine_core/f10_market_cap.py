"""Market-cap facts from F10 CSV rows (fact only, no size classification).

Legacy engine-next reads ``financial.circulating_market_cap / 1e8`` from its
F10 service and stores ``0.0`` on failure; the ``>= 300 yi`` "big cap" class
in ``stock_profile`` is a strategy threshold and is not part of this fact.

Evidence (2026-10-13, F10 CSV captured 2026-09-14, 5,574 rows): total market
cap / (total shares * Q2 pre-close) has median 1.04 (p10 0.92, p90 1.21) over
5,159 symbols, i.e. the columns are yuan and shares, not 10k-yuan or yi.  The
file carries no as-of of its own, so ``as_of`` is whatever the caller can
prove (the capture date); with ``as_of=None`` the fact is PARTIAL.

A missing, non-numeric or non-positive value is ``None``, never ``0``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Iterable, Mapping, Optional, Tuple

from .contracts import canonical_hash
from .facts import FactStatus

F10_MARKET_CAP_CONTRACT_VERSION = "F10MarketCapV1"
_COLUMNS = {
    "symbol": "股票代码",
    "name": "股票简称",
    "total_market_cap_yuan": "总市值",
    "circulating_market_cap_yuan": "a股市值(不含限售股)",
    "total_shares": "总股本",
    "float_shares": "流通a股",
}


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).strip().replace(",", ""))
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _symbol(value: Any) -> Optional[str]:
    text = str(value or "").strip().split(".")[0]
    return text if len(text) == 6 and text.isdigit() else None


@dataclass(frozen=True)
class F10MarketCaps:
    rows: Mapping[str, Mapping[str, Any]]
    status: FactStatus
    as_of: Optional[str]
    input_row_count: int
    invalid_symbol_rows: int
    duplicate_symbols: Tuple[str, ...]
    missing_cap_symbols: Tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "content_hash", canonical_hash({
            "contract": F10_MARKET_CAP_CONTRACT_VERSION, "as_of": self.as_of,
            "rows": {k: dict(v) for k, v in sorted(self.rows.items())},
        }))

    def circulating_market_cap_yi(self, symbol: str) -> Optional[float]:
        value = (self.rows.get(symbol) or {}).get("circulating_market_cap_yuan")
        return None if value is None else value / 1e8


def normalize_f10_market_caps(
    rows: Iterable[Mapping[str, Any]], *, as_of: Optional[str] = None
) -> F10MarketCaps:
    out: dict = {}
    duplicates: set = set()
    invalid = total = 0
    for raw in rows:
        total += 1
        symbol = _symbol(raw.get(_COLUMNS["symbol"]))
        if symbol is None:
            invalid += 1
            continue
        if symbol in out:
            duplicates.add(symbol)
            continue
        out[symbol] = {
            "symbol": symbol,
            "name": (str(raw.get(_COLUMNS["name"]) or "").strip() or None),
            "total_market_cap_yuan": _number(raw.get(_COLUMNS["total_market_cap_yuan"])),
            "circulating_market_cap_yuan": _number(raw.get(_COLUMNS["circulating_market_cap_yuan"])),
            "total_shares": _number(raw.get(_COLUMNS["total_shares"])),
            "float_shares": _number(raw.get(_COLUMNS["float_shares"])),
        }
    for symbol in duplicates:
        # neither copy of a duplicated code is trustworthy
        out.pop(symbol, None)
    missing = tuple(sorted(s for s, r in out.items() if r["circulating_market_cap_yuan"] is None))
    if not out:
        status = FactStatus.UNAVAILABLE
    elif as_of is None or missing or invalid or duplicates:
        status = FactStatus.PARTIAL
    else:
        status = FactStatus.READY
    return F10MarketCaps(
        rows=out, status=status, as_of=as_of, input_row_count=total,
        invalid_symbol_rows=invalid, duplicate_symbols=tuple(sorted(duplicates)),
        missing_cap_symbols=missing,
    )


def market_cap_unit_check(
    caps: F10MarketCaps, reference_price_yuan: Mapping[str, float]
) -> Mapping[str, Any]:
    """Median of ``total_market_cap / (total_shares * price)`` over the cohort.

    ~1 means yuan and shares (a 10k-yuan column would give ~1e-4).  Returned as
    evidence; it does not change any value.
    """

    ratios = []
    for symbol, row in caps.rows.items():
        price = reference_price_yuan.get(symbol)
        cap, shares = row["total_market_cap_yuan"], row["total_shares"]
        if cap and shares and price and price > 0:
            ratios.append(cap / (shares * price))
    if not ratios:
        return {"count": 0, "median": None, "units_consistent": None}
    m = median(ratios)
    return {"count": len(ratios), "median": m, "units_consistent": 0.5 <= m <= 2.0}


__all__ = [
    "F10_MARKET_CAP_CONTRACT_VERSION", "F10MarketCaps",
    "market_cap_unit_check", "normalize_f10_market_caps",
]
