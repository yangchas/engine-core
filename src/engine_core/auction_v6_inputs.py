"""Minimal auction report inputs assembled from the frozen V6 stock facts."""
from __future__ import annotations

from typing import Any, Mapping, Sequence


def prepare_auction_v6_inputs(*, trade_date: str, asof: str,
                              stock_facts: Sequence[Mapping[str, Any]],
                              stock_names: Mapping[str, str] | None = None,
                              fallback_stocks: Sequence[Mapping[str, Any]] = (),
                              limitations: str = "") -> dict[str, Any]:
    """Build the auction payload without legacy snapshots, chapters, or HTML."""
    names = stock_names or {}
    stocks = []
    amounts = []
    changes = []
    for fact in stock_facts:
        symbol = str(fact.get("symbol") or "").zfill(6)
        if not symbol.strip("0"):
            continue
        amount = fact.get("auction_amount_yuan")
        change = fact.get("pct_change")
        seal = fact.get("seal") if isinstance(fact.get("seal"), Mapping) else {}
        row = {
            "symbol": symbol,
            "name": fact.get("name") or names.get(symbol) or "名称未知",
            "auction_amount_yuan": amount,
            "amount_rank": fact.get("amount_rank"),
            "p0925_pct": change,
            "auction_pct": change,
            "valid_price": fact.get("valid_price"),
            "auction_price_status": "READY" if fact.get("valid_price") else "UNKNOWN",
            "auction_limit_state": 1 if fact.get("limit_up_candidate") else None,
            "in_yest_pool": False,
            "lb_days": None,
            "seal_amount_yuan": seal.get("seal_amount_yuan"),
            "seal_status": seal.get("status", "UNKNOWN"),
            "plates": [],
        }
        stocks.append(row)
        if isinstance(amount, (int, float)):
            amounts.append(float(amount))
        if isinstance(change, (int, float)):
            changes.append(float(change))
    if not stocks:
        stocks = [dict(row) for row in fallback_stocks if isinstance(row, Mapping)]
    total = sum(amounts) if amounts else None
    snapshot = {
        "label": "0925", "asof": asof, "status": "READY" if stock_facts else "MISSING",
        "symbol_count": len(stock_facts) or None,
        "priced_symbol_count": sum(bool(row.get("valid_price")) for row in stock_facts) or None,
        "market_match_amount_yuan": total,
        "median_pct_change": sorted(changes)[len(changes) // 2] if changes else None,
        "top_stocks_by_amount": sorted(stocks,
            key=lambda row: (row.get("auction_amount_yuan") is None,
                             -(row.get("auction_amount_yuan") or 0), row["symbol"]))[:20],
    }
    return {
        "trade_date": trade_date, "stage": "auction_0926", "status": snapshot["status"],
        "snapshots": [snapshot], "stocks": stocks, "sections": [], "conclusion": "",
        "history": None, "chapters": {}, "manifest": {"inputs": {}, "assembly": "v6"},
        "limitations": limitations,
        "method": {"source": "auction_stock_facts", "missing": "缺字段保持 null，不补 0",
                   "limitations": limitations},
    }
