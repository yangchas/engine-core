"""Auction summary aggregates from TD ``auction_snapshot_v2`` rows.

Pure.  On 2026-10-13 this function reproduced the legacy
``market:auction:{date}:{tag}`` summary for 0920, 0924 and 0925 on all 10
compared fields (counts, breadth, amount, limit up/down, seal amount), so the
legacy summary is exactly this aggregation of the frozen TD rows:

* candidate: any of ``match_amt``, ``rest_bid_amt``, ``rest_ask_amt`` > 0
* valid: candidate with ``px_milli > 0`` (``chg_bp`` present); the rest are
  unavailable
* positive / negative / flat: sign of ``chg_bp`` among valid candidates
* amount: sum of ``match_amt_yuan`` over candidates
* limit up / down: ``limit_state`` 1 / -1 over candidates; seal amount: sum of
  ``rest_bid_amt_yuan`` where ``limit_state == 1``

A missing value is never treated as 0: a candidate whose amount fields are
absent is counted in ``unknown_amount_rows`` and excluded from the sums.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

TD_AUCTION_SUMMARY_CONTRACT_VERSION = "TdAuctionSummaryV1"


def _pos(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _num(value: Any) -> Optional[float]:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def derive_td_auction_summary(
    rows: Iterable[Mapping[str, Any]], *, tag: str
) -> Mapping[str, Any]:
    if tag not in ("0920", "0924", "0925"):
        raise ValueError("tag must be 0920, 0924 or 0925")
    candidates = []
    seen_symbols: set = set()
    duplicates = 0
    for row in rows:
        if str(row.get("auction_tag")) != tag:
            continue
        symbol = row.get("symbol")
        if symbol in seen_symbols:
            duplicates += 1
            continue
        seen_symbols.add(symbol)
        if any(_pos(row.get(k)) for k in ("match_amt_yuan", "rest_bid_amt_yuan", "rest_ask_amt_yuan")):
            candidates.append(row)
    valid = [r for r in candidates if _pos(r.get("px_milli")) and _num(r.get("chg_bp")) is not None]
    amount_rows = [r for r in candidates if _num(r.get("match_amt_yuan")) is not None]
    seal_rows = [r for r in candidates if r.get("limit_state") == 1]
    unknown_amount_rows = len(candidates) - len(amount_rows)
    known_seal_rows = [r for r in seal_rows if _num(r.get("rest_bid_amt_yuan")) is not None]
    unknown_seal_rows = len(seal_rows) - len(known_seal_rows)
    return {
        "contract": TD_AUCTION_SUMMARY_CONTRACT_VERSION,
        "tag": tag,
        "stock_count": len(candidates),
        "valid_stock_count": len(valid),
        "unavailable_stock_count": len(candidates) - len(valid),
        "positive_count": sum(1 for r in valid if r["chg_bp"] > 0),
        "negative_count": sum(1 for r in valid if r["chg_bp"] < 0),
        "flat_count": sum(1 for r in valid if r["chg_bp"] == 0),
        "auction_amount_yuan": (
            sum(int(r["match_amt_yuan"]) for r in amount_rows)
            if unknown_amount_rows == 0 else None
        ),
        "limit_up_count": len(seal_rows),
        "limit_down_count": sum(1 for r in candidates if r.get("limit_state") == -1),
        "limit_up_seal_amount_yuan": (
            sum(int(r["rest_bid_amt_yuan"]) for r in known_seal_rows)
            if unknown_seal_rows == 0 else None
        ),
        "unknown_amount_rows": unknown_amount_rows,
        "unknown_seal_rows": unknown_seal_rows,
        "duplicate_symbol_rows": duplicates,
    }


__all__ = ["TD_AUCTION_SUMMARY_CONTRACT_VERSION", "derive_td_auction_summary"]
