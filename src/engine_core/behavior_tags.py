"""Descriptive auction behavior tags built from 0920/0924/0925 snapshots.

Tags describe observed price and matching-amount-share changes only. They do
not infer trader intent or combine fields into a score.
"""
from __future__ import annotations

from math import isfinite
from statistics import median
from typing import Any, Mapping


# Exact mathematical sign boundary. Any materially significant threshold needs
# calibration; these signs are descriptive and are not a significance filter.
DIRECTION_EPSILON = 0.0
SIGNIFICANCE_THRESHOLD_STATUS = "待校准"
_SNAPSHOTS = ("0920", "0924", "0925")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if isfinite(result) else None


def _get_number(row: Mapping[str, Any] | None, key: str) -> float | None:
    if not isinstance(row, Mapping):
        return None
    return _number(row.get(key))


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _sign(value: float | None) -> int | None:
    if value is None:
        return None
    if value > DIRECTION_EPSILON:
        return 1
    if value < -DIRECTION_EPSILON:
        return -1
    return 0


def _direction(price_delta: float | None, share_delta: float | None) -> str | None:
    price_sign, share_sign = _sign(price_delta), _sign(share_delta)
    if price_sign is None or share_sign is None:
        return None
    if price_sign == 0 or share_sign == 0:
        return "平"
    if price_sign > 0 and share_sign > 0:
        return "价升份额升(抢筹候选)"
    if price_sign < 0 and share_sign > 0:
        return "价降份额升(放量走弱)"
    if price_sign > 0 and share_sign < 0:
        return "价升份额降"
    return "价降份额降"


def _pressure(snapshot: Mapping[str, Any] | None) -> float | None:
    bid = _get_number(snapshot, "rb")
    ask = _get_number(snapshot, "ra")
    if bid is None or ask is None:
        return None
    return _ratio(bid - ask, bid + ask)


def calculate_stock_tags(stock: Mapping[str, Any], market: Mapping[str, Any]) -> dict[str, Any]:
    """Return descriptive tags for one stock.

    Input snapshots are keyed by ``0920``, ``0924`` and ``0925``. Each may
    contain ``pct`` (percent points), ``m`` (matching amount, yuan), ``rb`` and
    ``ra`` (unmatched bid/ask amount), ``limit_state`` (-1/0/1), and ``status``
    (for example ``FINAL`` or ``INDICATIVE``). Optional stock fields are
    ``prev_day_amount`` and ``plates_0925`` (plate -> equal-weight pct).
    Market snapshots contain the corresponding total matching amount ``m``.
    """
    snapshots = {tag: stock.get(tag) if isinstance(stock.get(tag), Mapping) else {}
                 for tag in _SNAPSHOTS}
    shares: dict[str, float | None] = {}
    pressures: dict[str, float | None] = {}
    for tag, snapshot in snapshots.items():
        market_snapshot = market.get(tag) if isinstance(market, Mapping) else None
        amount = _get_number(snapshot, "m")
        market_amount = _get_number(market_snapshot, "m")
        ratio = _ratio(amount, market_amount)
        shares[tag] = ratio * 10_000 if ratio is not None else None
        pressures[tag] = _pressure(snapshot)

    pct24 = _get_number(snapshots["0924"], "pct")
    pct25 = _get_number(snapshots["0925"], "pct")
    d_pct = pct25 - pct24 if pct24 is not None and pct25 is not None else None
    share24, share25 = shares["0924"], shares["0925"]
    d_share = share25 - share24 if share24 is not None and share25 is not None else None

    state24 = _number(snapshots["0924"].get("limit_state"))
    state25 = _number(snapshots["0925"].get("limit_state"))
    was_sealed = (state24 in (-1, 1)) if state24 is not None else None
    last_minute_seal = (state24 == 0 and state25 in (-1, 1)
                        if state24 is not None and state25 is not None else None)

    seal_amount = None
    seal_over_match = None
    seal_over_prev_day = None
    if state25 in (-1, 1):
        remainder_key = "rb" if state25 == 1 else "ra"
        seal_amount = _get_number(snapshots["0925"], remainder_key)
        seal_over_match = _ratio(seal_amount, _get_number(snapshots["0925"], "m"))
        seal_over_prev_day = _ratio(seal_amount, _get_number(stock, "prev_day_amount"))

    plates = stock.get("plates_0925")
    plate_values = [_number(v) for v in plates.values()] if isinstance(plates, Mapping) else []
    plate_values = [v for v in plate_values if v is not None]
    strongest_plate_pct = max(plate_values) if plate_values else None
    rel = pct25 - strongest_plate_pct if pct25 is not None and strongest_plate_pct is not None else None

    result: dict[str, Any] = {
        "share_0924": share24,
        "share_0925": share25,
        "d_share_bp": d_share,
        "d_pct_pp": d_pct,
        "direction": _direction(d_pct, d_share),
        "direction_significance": SIGNIFICANCE_THRESHOLD_STATUS,
        "pressure_0920": pressures["0920"],
        "pressure_0924": pressures["0924"],
        "pressure_0925": pressures["0925"],
        "pressure": pressures["0925"],
        "seal_amount": seal_amount,
        "seal_over_match": seal_over_match,
        "seal_over_prev_day_amount": seal_over_prev_day,
        "was_sealed_0924": was_sealed,
        "last_minute_seal": last_minute_seal,
        "rel_to_strongest_plate_pp": rel,
        "outrun": rel >= 2.0 if rel is not None else None,
    }
    for tag in _SNAPSHOTS:
        result[f"pct_{tag}"] = _get_number(snapshots[tag], "pct")
        result[f"status_{tag}"] = snapshots[tag].get("status")
    return result


def _stocks_iter(stocks: Any):
    if isinstance(stocks, Mapping):
        return stocks.items()
    if isinstance(stocks, (list, tuple)):
        return ((str(i), row) for i, row in enumerate(stocks))
    return ()


def summarize(market: Mapping[str, Any], stocks: Any) -> dict[str, Any]:
    """Summarize direction counts and unthresholded sealed-amount ratios."""
    tags = [calculate_stock_tags(row, market) for _, row in _stocks_iter(stocks)
            if isinstance(row, Mapping)]
    direction_counts: dict[str, int] = {
        "价升份额升(抢筹候选)": 0,
        "价降份额升(放量走弱)": 0,
        "价升份额降": 0,
        "价降份额降": 0,
        "平": 0,
        "未知": 0,
    }
    for row in tags:
        direction = row["direction"]
        direction_counts[direction if direction is not None else "未知"] += 1

    sealed_stocks = [stock for _, stock in _stocks_iter(stocks)
                     if isinstance(stock, Mapping) and _limit_side(stock) is not None]
    sealed = [calculate_stock_tags(stock, market) for stock in sealed_stocks]
    amounts = [row["seal_amount"] for row in sealed if row["seal_amount"] is not None]
    over_match = [row["seal_over_match"] for row in sealed if row["seal_over_match"] is not None]
    over_prev = [row["seal_over_prev_day_amount"] for row in sealed
                 if row["seal_over_prev_day_amount"] is not None]
    # Side counts follow explicit limit_state; missing/zero is never counted as sealed.
    return {
        "direction_counts": direction_counts,
        "seal_quality": {
            "count": len(sealed),
            "up_count": sum(1 for _, stock in _stocks_iter(stocks)
                             if isinstance(stock, Mapping) and _limit_side(stock) == 1),
            "down_count": sum(1 for _, stock in _stocks_iter(stocks)
                               if isinstance(stock, Mapping) and _limit_side(stock) == -1),
            "seal_amount_median": median(amounts) if amounts else None,
            "seal_amount_min": min(amounts) if amounts else None,
            "seal_amount_max": max(amounts) if amounts else None,
            "seal_over_match_median": median(over_match) if over_match else None,
            "seal_over_prev_day_amount_median": median(over_prev) if over_prev else None,
        },
        "stock_count": len(tags),
    }


def _limit_side(stock: Mapping[str, Any]) -> int | None:
    snapshot = stock.get("0925")
    state = _get_number(snapshot, "limit_state")
    return int(state) if state in (-1, 1) else None
