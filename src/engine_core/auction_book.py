"""Derive call-auction and post-call values from the three-level tick book."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


_ZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class AuctionBookTick:
    ts_ms: int
    px_milli: int | None
    bp1: int | None
    ap1: int | None
    bv1: int | None
    av1: int | None
    bv2: int | None
    av2: int | None
    amt_yuan: int | None
    vol_units: int | None


@dataclass(frozen=True)
class AuctionBookState:
    effective_price_milli: int
    matched_volume: int
    match_amt_yuan: int
    rest_bid_amt_yuan: int
    rest_ask_amt_yuan: int
    imbalance_side: str
    phase: str
    flags: tuple[str, ...] = ()


def _nonnegative(value: int | None) -> int:
    return max(0, int(value or 0))


def derive_auction_book(tick: AuctionBookTick) -> AuctionBookState:
    local = datetime.fromtimestamp(tick.ts_ms / 1000, _ZONE)
    hms = local.hour * 10000 + local.minute * 100 + local.second
    phase = "CALL" if hms < 92500 else "RESULT" if hms < 92600 else "NONE"
    bp1, ap1 = _nonnegative(tick.bp1), _nonnegative(tick.ap1)
    bv1, av1 = _nonnegative(tick.bv1), _nonnegative(tick.av1)
    bv2, av2 = _nonnegative(tick.bv2), _nonnegative(tick.av2)
    if hms >= 92500:
        price = _nonnegative(tick.px_milli)
    else:
        price = bp1 if bp1 > 0 and ap1 > 0 and bp1 == ap1 else 0
    flags: list[str] = []
    if hms < 92500 and price == 0:
        flags.append("NO_MATCH_PRICE")
        matched = rest_bid = rest_ask = 0
    else:
        matched = (int(tick.amt_yuan) if hms >= 92500 and _nonnegative(tick.amt_yuan) > 0
                   else price * _nonnegative(tick.vol_units) * 100 // 1000
                   if hms >= 92500 and _nonnegative(tick.vol_units) > 0
                   else min(bv1, av1) * price * 100 // 1000 if price > 0 else 0)
        rest_bid = bv2 * bp1 * 100 // 1000
        rest_ask = av2 * ap1 * 100 // 1000
    side = "UNKNOWN" if price <= 0 else (
        "BID" if bv2 > av2 else "ASK" if av2 > bv2 else "BALANCED")
    matched_volume = _nonnegative(tick.vol_units) if hms >= 92500 else min(bv1, av1)
    return AuctionBookState(price, matched_volume, matched, rest_bid, rest_ask, side, phase,
                            tuple(flags))


__all__ = ["AuctionBookTick", "AuctionBookState", "derive_auction_book"]
