"""Objective per-stock extremes for the auction fact report.

The legacy ``【竞价极值榜】`` ranks by a composite score with hand-set weights
and attaches trading conclusions (``_extreme_score`` / ``_extreme_type_label``
in engine-next).  That is strategy output, not a fact.  This module only
orders stocks by single, named, unit-explicit measures; it adds no score, no
label and no conclusion.  A stock lacking the measure is counted as excluded,
never ranked as zero, so a ranking with exclusions is PARTIAL.

Measures (all from one already-read Q2 cohort):

* ``auction_amount_top``  - ``am`` (yuan), largest first
* ``auction_gain_top`` / ``auction_drop_top`` - 0925 change in basis points,
  ``trunc((a25 - pc) * 10000 / pc)``, the TD ``chg_bp`` rule
* ``limit_up_seal_top``   - ``br`` (yuan) among ``ls == 1``, largest first
* ``amount_delta_top``    - 0924 -> 0925 auction amount delta (yuan), only
  when the caller supplies delta rows
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Tuple

from .contracts import canonical_hash
from .facts import FactStatus

STOCK_EXTREMES_CONTRACT_VERSION = "StockAuctionExtremesV1"
_UNITS = {
    "auction_amount_top": "yuan",
    "auction_gain_top": "basis_points",
    "auction_drop_top": "basis_points",
    "limit_up_seal_top": "yuan",
    "amount_delta_top": "yuan",
}


def _pos_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


@dataclass(frozen=True)
class StockExtremes:
    trade_date: str
    status: FactStatus
    top_n: int
    candidate_count: int
    rankings: Mapping[str, Tuple[Mapping[str, Any], ...]]
    excluded_counts: Mapping[str, int]
    units: Mapping[str, str]
    notes: Tuple[str, ...] = ()
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        payload = {
            "contract": STOCK_EXTREMES_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "status": str(self.status),
            "top_n": self.top_n,
            "candidate_count": self.candidate_count,
            "rankings": {k: list(v) for k, v in self.rankings.items()},
            "excluded_counts": dict(self.excluded_counts),
        }
        object.__setattr__(self, "content_hash", canonical_hash(payload))

    def as_mapping(self) -> Mapping[str, Any]:
        return {
            "contract": STOCK_EXTREMES_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "status": self.status,
            "top_n": self.top_n,
            "candidate_count": self.candidate_count,
            "rankings": {k: [dict(r) for r in v] for k, v in self.rankings.items()},
            "excluded_counts": dict(self.excluded_counts),
            "units": dict(self.units),
            "notes": self.notes,
            "content_hash": self.content_hash,
        }


def derive_stock_extremes(
    quotes: Mapping[str, Any],
    *,
    trade_date: str,
    top_n: int = 10,
    recovered_anchor_milli: Optional[Mapping[str, int]] = None,
    amount_delta_by_symbol: Optional[Mapping[str, Optional[float]]] = None,
) -> StockExtremes:
    if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1:
        raise ValueError("top_n must be a positive integer")
    recovered = recovered_anchor_milli or {}
    candidates = []
    for symbol in sorted(quotes):
        quote = quotes[symbol]
        am = getattr(quote, "auction_amount_yuan", None)
        br = getattr(quote, "auction_bid_amount_yuan", None)
        ar = getattr(quote, "auction_ask_amount_yuan", None)
        if any(_pos_int(v) for v in (am, br, ar)):
            candidates.append(symbol)

    def name(symbol: str) -> Optional[str]:
        return getattr(quotes[symbol], "name", None)

    def anchor(symbol: str) -> Optional[int]:
        value = getattr(quotes[symbol], "auction_anchor_0925_price_milli", None)
        if _pos_int(value):
            return value
        return recovered.get(symbol) if _pos_int(recovered.get(symbol)) else None

    def change_bp(symbol: str) -> Optional[int]:
        a, pc = anchor(symbol), getattr(quotes[symbol], "pre_close_milli", None)
        if _pos_int(a) and _pos_int(pc):
            return int((a - pc) * 10000 / pc)
        return None

    def amount(symbol: str) -> Optional[int]:
        v = getattr(quotes[symbol], "auction_amount_yuan", None)
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

    def seal(symbol: str) -> Optional[int]:
        quote = quotes[symbol]
        if getattr(quote, "limit_state", None) != 1:
            return None
        v = getattr(quote, "auction_bid_amount_yuan", None)
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

    rankings: dict = {}
    excluded: dict = {}

    def rank(key: str, symbols: Iterable[str], value, *, descending: bool, skip_unknown: bool = True) -> None:
        scored, unknown = [], 0
        for symbol in symbols:
            v = value(symbol)
            if v is None:
                unknown += 1
            else:
                scored.append((symbol, v))
        scored.sort(key=lambda item: ((-item[1] if descending else item[1]), item[0]))
        rankings[key] = tuple(
            {"rank": i, "symbol": s, "name": name(s), "value": v}
            for i, (s, v) in enumerate(scored[:top_n], 1)
        )
        excluded[key] = unknown

    rank("auction_amount_top", candidates, amount, descending=True)
    rank("auction_gain_top", candidates, change_bp, descending=True)
    rank("auction_drop_top", candidates, change_bp, descending=False)
    limit_up = [
        s for s in candidates
        if getattr(quotes[s], "limit_state", None) == 1
        or getattr(quotes[s], "limit_state", None) is None
    ]
    rank("limit_up_seal_top", limit_up, seal, descending=True)
    notes = []
    if amount_delta_by_symbol is not None:
        rank("amount_delta_top", candidates, lambda s: amount_delta_by_symbol.get(s), descending=True)
    else:
        notes.append("amount_delta_top omitted: no 0924 amount snapshot supplied")
    # a ranking that skipped stocks lacking its measure cannot claim a full cohort
    partial = any(count for key, count in excluded.items() if key != "limit_up_seal_top") or excluded.get("limit_up_seal_top", 0)
    status = FactStatus.UNAVAILABLE if not candidates else (FactStatus.PARTIAL if partial else FactStatus.READY)
    return StockExtremes(
        trade_date=trade_date, status=status, top_n=top_n, candidate_count=len(candidates),
        rankings=rankings, excluded_counts=excluded,
        units={key: _UNITS[key] for key in rankings}, notes=tuple(notes),
    )


__all__ = ["STOCK_EXTREMES_CONTRACT_VERSION", "StockExtremes", "derive_stock_extremes"]
