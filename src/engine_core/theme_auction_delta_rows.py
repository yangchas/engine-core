"""Build canonical 0924->0925 theme-delta rows from raw auction rows.

``build_theme_auction_delta_facts`` consumes already-normalized delta rows.
This module is the pure, source-independent adapter from TD-shaped auction
rows (``auction_tag``/``match_amt_yuan``/``rest_bid_amt_yuan``/``chg_bp``) and
a symbol->plate mapping to those rows and to theme weights.  No I/O.

Rules: a delta is emitted only when both operands are present (never zero
filled); a ratio with a non-positive previous amount is ``None`` (undefined,
not ``0``); only symbols observed in both tags produce a row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from .auction import normalize_auction_change_bp_to_pct
from .contracts import canonical_hash
from .previous_day_limit_current_rows import _amount_yuan, _as_mapping, _dashed_date
from .q2 import normalize_symbol
from .theme_weights import resolve_legacy_theme_weights

THEME_DELTA_ROWS_CONTRACT_VERSION = "ThemeAuctionDeltaRowsV1"


@dataclass(frozen=True)
class ThemeDeltaRows:
    trade_date: str
    from_tag: str
    to_tag: str
    rows: Tuple[Mapping[str, Any], ...]
    from_symbol_count: int
    to_symbol_count: int
    ignored_row_count: int
    content_hash: str

    def as_trace(self) -> Mapping[str, Any]:
        return {
            "contract": THEME_DELTA_ROWS_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "from_tag": self.from_tag,
            "to_tag": self.to_tag,
            "row_count": len(self.rows),
            "from_symbol_count": self.from_symbol_count,
            "to_symbol_count": self.to_symbol_count,
            "ignored_row_count": self.ignored_row_count,
            "content_hash": self.content_hash,
        }


def _tag_values(
    trade_date: str, td_rows: Iterable[Any], tag: str
) -> Tuple[dict, int]:
    values: dict = {}
    ignored = 0
    for raw in td_rows:
        row = _as_mapping(raw)
        if row is None:
            ignored += 1
            continue
        if str(row.get("auction_tag") or row.get("tag") or "") != tag:
            continue
        if _dashed_date(row.get("trade_date")) != trade_date:
            ignored += 1
            continue
        try:
            symbol = normalize_symbol(str(row.get("symbol", "")))
        except (TypeError, ValueError):
            ignored += 1
            continue
        values[symbol] = {
            "amount": _amount_yuan(row.get("match_amt_yuan", row.get("auction_amount_yuan"))),
            "bid": _amount_yuan(row.get("rest_bid_amt_yuan", row.get("bid_amount_yuan"))),
            "pct": normalize_auction_change_bp_to_pct(row.get("chg_bp")),
        }
    return values, ignored


def _delta(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous is None:
        return None
    return float(current) - float(previous)


def build_theme_delta_rows(
    trade_date: str,
    td_rows: Iterable[Any],
    *,
    from_tag: str = "0924",
    to_tag: str = "0925",
    evidence_ref_prefix: Optional[str] = None,
) -> ThemeDeltaRows:
    requested = _dashed_date(trade_date)
    if requested is None:
        raise ValueError("trade_date must be strict YYYY-MM-DD")
    materialized = list(td_rows)
    previous, ignored_a = _tag_values(requested, materialized, from_tag)
    current, ignored_b = _tag_values(requested, materialized, to_tag)
    prefix = (evidence_ref_prefix or "td://auction/%s/%s-%s" % (requested, from_tag, to_tag)).rstrip("/")
    rows = []
    for symbol in sorted(set(previous) & set(current)):
        before, after = previous[symbol], current[symbol]
        ratio = None
        if after["amount"] is not None and before["amount"] is not None and before["amount"] > 0:
            ratio = after["amount"] / before["amount"]
        rows.append(
            {
                "symbol": symbol,
                "tag": to_tag,
                "previous_tag": from_tag,
                "amount_yuan": after["amount"],
                "amount_delta_yuan": _delta(after["amount"], before["amount"]),
                "bid_amount_delta_yuan": _delta(after["bid"], before["bid"]),
                "change_pct_delta": _delta(after["pct"], before["pct"]),
                "amount_ratio": ratio,
                "evidence_ref": "%s/%s" % (prefix, symbol),
            }
        )
    rows_t = tuple(rows)
    return ThemeDeltaRows(
        trade_date=requested,
        from_tag=from_tag,
        to_tag=to_tag,
        rows=rows_t,
        from_symbol_count=len(previous),
        to_symbol_count=len(current),
        ignored_row_count=max(ignored_a, ignored_b),
        content_hash=canonical_hash({"trade_date": requested, "rows": rows_t}),
    )


def theme_weights_from_plate_mapping(
    plate_by_symbol: Mapping[str, Any],
    symbols: Iterable[str],
    *,
    real_plate_names_by_symbol: Optional[Mapping[str, Sequence[Any]]] = None,
) -> dict:
    """Return legacy theme weights per symbol; unmapped symbols get ``()``."""

    extra = real_plate_names_by_symbol or {}
    return {
        symbol: resolve_legacy_theme_weights(
            plate_by_symbol.get(symbol), tuple(extra.get(symbol, ()))
        )
        for symbol in symbols
    }


__all__ = [
    "THEME_DELTA_ROWS_CONTRACT_VERSION",
    "ThemeDeltaRows",
    "build_theme_delta_rows",
    "theme_weights_from_plate_mapping",
]
