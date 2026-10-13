"""Strategy-free auction limit-price and seal facts from TD snapshot rows."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
from typing import Any, Mapping

from .plate_kind import classify_plate_kind
from .plate_membership import PlateHierarchy, StockPlateMemberships, members_of


@dataclass(frozen=True)
class StockAuctionLimitFact:
    symbol: str
    trade_date: str
    pre_close: float | None
    prev_day_amount: float | None
    float_mkt_cap: float | None
    price_milli_0920: int | None
    match_amt_0920: float | None
    rest_bid_0920: float | None
    rest_ask_0920: float | None
    price_milli_0924: int | None
    match_amt_0924: float | None
    rest_bid_0924: float | None
    rest_ask_0924: float | None
    price_milli_0925: int | None
    match_amt_0925: float | None
    rest_bid_0925: float | None
    rest_ask_0925: float | None
    lock_ratio_0920: float | None
    lock_ratio_0924: float | None
    price_drift_pct_0920_0925: float | None
    at_limit_up_0925: bool | None
    at_limit_down_0925: bool | None
    seal_amt_0920: float | None
    seal_amt_0924: float | None
    seal_amt_0925: float | None
    seal_strength_prev_amt: float | None
    seal_strength_float: float | None
    seal_growth_0920_0925: float | None
    seal_trajectory: tuple[float | None, float | None, float | None]
    one_word_hint: bool | None
    ls_0925: int | None
    flags: tuple[str, ...]


@dataclass(frozen=True)
class PlateAuctionLimitFact:
    plate: str
    member_count: int
    limit_up_count: int
    limit_down_count: int
    seal_mass_up: float | None
    seal_mass_down: float | None
    seal_to_match_ratio: float | None
    top1_share_of_seal_mass: float | None
    strong_seal_count: int
    flags: tuple[str, ...]


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> int | None:
    number = _finite(value)
    return int(number) if number is not None and number.is_integer() else None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _limit_pct(symbol: str) -> Decimal:
    if symbol.startswith(("688", "689", "30")):
        return Decimal("20")
    if symbol.startswith(("8", "4", "92")):
        return Decimal("30")
    return Decimal("10")


def _rounded_limit_price(pre_close: float, symbol: str, direction: int) -> Decimal | None:
    close = _decimal(pre_close)
    if close is None or close <= 0:
        return None
    multiplier = Decimal(1) + Decimal(direction) * _limit_pct(symbol) / Decimal(100)
    return (close * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _row_value(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def derive_stock_auction_limit_fact(
    *, symbol: str, trade_date: str, pre_close: float | None,
    prev_day_amount: float | None, snapshots: Mapping[str, Mapping[str, Any]],
    float_mkt_cap: float | None = None, one_word_threshold: float = 0.1,
) -> StockAuctionLimitFact:
    """Derive one stock's objective auction facts; absent inputs stay missing."""
    if not math.isfinite(float(one_word_threshold)) or one_word_threshold < 0:
        raise ValueError("one_word_threshold must be finite and nonnegative")
    fields: dict[str, Any] = {}
    for tag in ("0920", "0924", "0925"):
        row = snapshots.get(tag) or {}
        fields[f"price_milli_{tag}"] = _integer(_row_value(row, "px_milli", "price_milli"))
        fields[f"match_amt_{tag}"] = _finite(_row_value(row, "match_amt_yuan", "match_amt"))
        fields[f"rest_bid_{tag}"] = _finite(_row_value(row, "rest_bid_amt_yuan", "rest_bid"))
        fields[f"rest_ask_{tag}"] = _finite(_row_value(row, "rest_ask_amt_yuan", "rest_ask"))
    match25 = fields["match_amt_0925"]
    ratios = {
        tag: (fields[f"match_amt_{tag}"] / match25
              if fields[f"match_amt_{tag}"] is not None and match25 is not None and match25 > 0
              else None)
        for tag in ("0920", "0924")
    }
    price20, price25 = fields["price_milli_0920"], fields["price_milli_0925"]
    drift = ((price25 / price20 - 1.0) * 100.0
             if price20 is not None and price25 is not None and price20 > 0 else None)
    price25_yuan = Decimal(price25) / Decimal(1000) if price25 is not None else None
    up_limit = _rounded_limit_price(pre_close, symbol, 1) if pre_close is not None else None
    down_limit = _rounded_limit_price(pre_close, symbol, -1) if pre_close is not None else None
    at_up = price25_yuan == up_limit if price25_yuan is not None and up_limit is not None else None
    at_down = price25_yuan == down_limit if price25_yuan is not None and down_limit is not None else None
    seals: list[float | None] = []
    for tag in ("0920", "0924", "0925"):
        tag_price = fields[f"price_milli_{tag}"]
        tag_price_yuan = Decimal(tag_price) / Decimal(1000) if tag_price is not None else None
        tag_at_up = tag_price_yuan == up_limit if tag_price_yuan is not None and up_limit is not None else None
        tag_at_down = tag_price_yuan == down_limit if tag_price_yuan is not None and down_limit is not None else None
        if tag_at_up:
            seals.append(fields[f"rest_bid_{tag}"])
        elif tag_at_down:
            seals.append(fields[f"rest_ask_{tag}"])
        else:
            seals.append(None)
    seal25, seal20 = seals[2], seals[0]
    prev_amount = _finite(prev_day_amount)
    float_cap = _finite(float_mkt_cap)
    flags: list[str] = []
    if pre_close is None or _finite(pre_close) is None or float(pre_close) <= 0:
        flags.append("NO_PRE_CLOSE")
    if prev_amount is None or prev_amount <= 0:
        flags.append("NO_PREV_AMOUNT")
    if not snapshots.get("0920"):
        flags.append("NO_0920")
    if seal20 is None or seal20 <= 0:
        flags.append("SEAL_ABSENT_AT_0920")
    one_word = None
    if at_up is True and match25 is not None and fields["rest_bid_0925"] is not None:
        total = match25 + fields["rest_bid_0925"]
        if total > 0:
            one_word = match25 / total < one_word_threshold
    ls = _integer(_row_value(snapshots.get("0925") or {}, "limit_state", "ls"))
    return StockAuctionLimitFact(
        symbol=symbol, trade_date=trade_date, pre_close=_finite(pre_close),
        prev_day_amount=prev_amount, float_mkt_cap=float_cap, **fields,
        lock_ratio_0920=ratios["0920"], lock_ratio_0924=ratios["0924"],
        price_drift_pct_0920_0925=drift, at_limit_up_0925=at_up,
        at_limit_down_0925=at_down, seal_amt_0920=seals[0], seal_amt_0924=seals[1],
        seal_amt_0925=seal25,
        seal_strength_prev_amt=(seal25 / prev_amount if seal25 is not None and prev_amount and prev_amount > 0 else None),
        seal_strength_float=(seal25 / float_cap if seal25 is not None and float_cap and float_cap > 0 else None),
        seal_growth_0920_0925=(seal25 / seal20 if seal25 is not None and seal20 is not None and seal20 > 0 else None),
        seal_trajectory=(seals[0], seals[1], seals[2]), one_word_hint=one_word,
        ls_0925=ls, flags=tuple(flags),
    )


def aggregate_plate_auction_limit_facts(
    stock_facts: Mapping[str, StockAuctionLimitFact], memberships: StockPlateMemberships,
    hierarchy: PlateHierarchy, *, strong_seal_threshold: float = 0.0196,
) -> tuple[PlateAuctionLimitFact, ...]:
    """Aggregate facts over distinct membership unions; unknown stock rows are excluded."""
    if not math.isfinite(float(strong_seal_threshold)) or strong_seal_threshold < 0:
        raise ValueError("strong_seal_threshold must be finite and nonnegative")
    plates = sorted({row.plate for rows in memberships.by_symbol.values() for row in rows
                     if row.kind in ("THEME", "GENERIC")
                     and classify_plate_kind(row.plate) in ("THEME", "GENERIC")})
    output: list[PlateAuctionLimitFact] = []
    for plate in plates:
        symbols = members_of(plate, memberships, hierarchy)
        facts = [stock_facts[symbol] for symbol in sorted(symbols) if symbol in stock_facts]
        up = [fact for fact in facts if fact.at_limit_up_0925 is True]
        down = [fact for fact in facts if fact.at_limit_down_0925 is True]
        up_seals = [fact.seal_amt_0925 for fact in up if fact.seal_amt_0925 is not None]
        down_seals = [fact.seal_amt_0925 for fact in down if fact.seal_amt_0925 is not None]
        observed_auction = any(fact.price_milli_0925 is not None or fact.match_amt_0925 is not None
                               for fact in facts)
        seal_up = (sum(up_seals) if len(up_seals) == len(up) else None) if up else (0.0 if observed_auction else None)
        seal_down = (sum(down_seals) if len(down_seals) == len(down) else None) if down else (0.0 if observed_auction else None)
        match_values = [fact.match_amt_0925 for fact in facts if fact.match_amt_0925 is not None]
        match_total = sum(match_values)
        strengths = [fact.seal_strength_prev_amt for fact in facts
                     if fact.seal_strength_prev_amt is not None]
        flags: list[str] = []
        missing = len(symbols) - len(facts)
        if missing:
            flags.append(f"MISSING_STOCK_FACTS:{missing}")
        if any(fact.seal_amt_0925 is None for fact in up + down):
            flags.append("MISSING_LIMIT_SEAL_AMOUNT")
        largest = max(up_seals, default=None)
        output.append(PlateAuctionLimitFact(
            plate=plate, member_count=len(symbols), limit_up_count=len(up), limit_down_count=len(down),
            seal_mass_up=seal_up, seal_mass_down=seal_down,
            seal_to_match_ratio=(seal_up / match_total if seal_up is not None and match_total > 0 else None),
            top1_share_of_seal_mass=(largest / seal_up if largest is not None and seal_up and seal_up > 0 else None),
            strong_seal_count=sum(value >= strong_seal_threshold for value in strengths),
            flags=tuple(flags),
        ))
    return tuple(output)


def stock_fact_to_dict(fact: StockAuctionLimitFact) -> dict[str, Any]:
    return asdict(fact)


def plate_fact_to_dict(fact: PlateAuctionLimitFact) -> dict[str, Any]:
    return asdict(fact)

