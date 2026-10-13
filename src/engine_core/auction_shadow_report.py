"""Compose the market auction fact report from one Q2 snapshot.

Source-independent: takes already-read Q2 quotes (live or sealed), the
late-final recoveries observed by ``AuctionAnchorSessionV1`` and a sealed
previous-day pool result.  Theme deltas are omitted because they need the
0924 snapshot; the report says so instead of inventing them.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Optional

from .clock import local_datetime_ms
from .contracts import DataResult
from .market_auction_report import MarketAuctionReport, build_market_auction_report
from .market_summary import derive_q2_auction_summary
from .previous_day_limit_current_rows import build_current_0925_rows_from_q2
from .previous_day_limit_feedback import build_previous_day_limit_feedback
from .previous_day_limit_structure import build_previous_day_limit_structure
from .stock_extremes import derive_stock_extremes


def build_shadow_auction_report(
    *,
    trade_date: str,
    quotes: Mapping[str, Any],
    observation_time_ms: int,
    recoveries: Mapping[str, Mapping[str, Any]] = {},
    previous_pool: Optional[DataResult] = None,
    data_origin: str = "LIVE_REDIS_READ_ONLY",
    event_id: str = "AUCTION_0925",
    source_id: str = "redis_q2_shadow",
    input_content_hash: str = "unrecorded",
    top_n: int = 10,
) -> MarketAuctionReport:
    patched = {}
    for symbol, quote in quotes.items():
        recovered = recoveries.get(symbol)
        anchor = getattr(quote, "auction_anchor_0925_price_milli", None)
        if recovered is not None and not (isinstance(anchor, int) and anchor > 0):
            quality = dict(quote.auction_anchor_field_quality)
            quality["a25"] = "PRESENT_VALUE"
            quote = replace(
                quote,
                auction_anchor_0925_price_milli=recovered["price_milli"],
                auction_anchor_field_quality=quality,
            )
        patched[symbol] = quote
    summary = derive_q2_auction_summary(
        patched, trade_date=trade_date, source_id=source_id,
        source_table="Q2:redis_projection", observation_time_ms=observation_time_ms,
        input_content_hash=input_content_hash,
    )
    notes = [
        "theme delta omitted: needs the 0924 snapshot (not supplied to the shadow reader)",
        "0925 anchors include %d symbols recovered from late ph==0 snapshots" % len(recoveries),
    ]
    if observation_time_ms < local_datetime_ms(trade_date, "09:25:00"):
        notes.append(
            "observed before 09:25:00: a25 is not produced yet, so valid/unavailable "
            "counts and prior-day returns describe the collecting phase, not the final auction"
        )
    structure = feedback = None
    if previous_pool is not None:
        current = build_current_0925_rows_from_q2(trade_date, patched)
        feedback = build_previous_day_limit_feedback(
            previous_pool, current.rows, current_trade_date=trade_date
        )
        structure = build_previous_day_limit_structure(previous_pool)
        notes.append("previous-day pool availability is unverified (previous-day data)")
        rejected = (previous_pool.data or {}).get("rejected_count") if previous_pool.data else None
        if isinstance(rejected, int) and rejected > 0:
            notes.append(
                "previous-day pool: %d row(s) rejected as invalid; counts exclude them" % rejected
            )
    else:
        notes.append("previous-day pool not supplied")
    extremes = derive_stock_extremes(patched, trade_date=trade_date, top_n=top_n)
    return build_market_auction_report(
        trade_date=trade_date, event_id=event_id, data_origin=data_origin,
        market_summary=summary, previous_limit_structure=structure,
        previous_limit_feedback=feedback, stock_extremes=extremes, top_n=top_n, notes=notes,
    )


__all__ = ["build_shadow_auction_report"]
