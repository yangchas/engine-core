"""Auction-selected stock cohort and later descriptive tracking."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


DEFAULT_K_ABS = 20
DEFAULT_K_AMOUNT = 20
DEFAULT_MIN_AUCTION_AMOUNT_YUAN = 30_000_000
DEFAULT_DIRECTION_THRESHOLD_PCT = 3.0


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _symbol(row: Mapping[str, Any]) -> str:
    value = str(row.get("symbol", row.get("code", "")) or "").strip()
    return value.zfill(6) if value.isdigit() and len(value) < 6 else value


def _unlimited(row: Mapping[str, Any]) -> bool:
    return bool(row.get("is_new_listing") or row.get("is_unlimited_price_band") or
                row.get("price_band_label") in {"新股/无涨跌幅限制", "新股/无涨跌停价"} or
                row.get("auction_price_status") in {"UNLIMITED", "无涨跌停价", "无涨跌幅限制"})


def _track(row: Mapping[str, Any]) -> dict[str, float | None]:
    auction = _number(row.get("p0925_pct", row.get("auction_pct")))
    result: dict[str, float | None] = {}
    for tag in ("0932", "0935", "0940", "0945"):
        result[f"{tag}_pct"] = _number(row.get(f"p{tag}_pct"))
    result["latest_pct"] = _number(row.get("latest_pct", row.get("realtime_pct")))
    result["close_pct"] = _number(row.get("close_pct"))
    for label in ("latest", "close"):
        value = result[f"{label}_pct"]
        result[f"delta_{label}_pp"] = value - auction if value is not None and auction is not None else None
    high, low = _number(row.get("high_pct")), _number(row.get("low_pct"))
    result["high_vs_auction_pp"] = high - auction if high is not None and auction is not None else None
    result["low_vs_auction_pp"] = low - auction if low is not None and auction is not None else None
    band = _number(row.get("limit_pct"))
    delta = result["delta_close_pp"]
    result["delta_close_over_band"] = delta / band if delta is not None and band else None
    return result


_KEEP_FIELDS = ("symbol", "name", "main_plate", "limit_pct", "auction_pct", "auction_amount_yuan",
                "auction_amount_bp", "auction_price_status", "auction_limit_state", "p0924_pct", "p0925_pct",
                "p0932_pct", "p0935_pct", "p0940_pct", "p0945_pct", "close_pct", "high_pct", "low_pct",
                "abs_rank", "amount_rank", "selection_reason", "auction_state", "tracking", "in_yest_pool",
                "lb_days", "is_unlimited_price_band")


def _slim_row(row: Any) -> Any:
    if not isinstance(row, Mapping):
        return row
    return {key: row[key] for key in _KEEP_FIELDS if key in row}


def _slim_result(result: dict[str, Any], stage: str | None = None) -> dict[str, Any]:
    """Keep the board compact: only displayed fields, no repeated full stock records."""
    for section in ("auction", "open", "close"):
        block = result.get(section)
        if not isinstance(block, dict):
            continue
        for key, value in list(block.items()):
            if isinstance(value, list) and value and isinstance(value[0], Mapping):
                block[key] = [_slim_row(item) for item in value]
    active = {"close": "close", "open": "open", "auction": "auction"}.get(stage or "")
    minimal = ("symbol", "name", "abs_rank", "amount_rank", "selection_reason")
    for section in ("auction", "open", "close"):
        block = result.get(section)
        if active is not None and section != active and isinstance(block, dict):
            if section == "auction" and active != "auction":
                block["amount_cohort"] = []
            for key in ("rows",):
                if isinstance(block.get(key), list):
                    block[key] = [{k: r.get(k) for k in minimal} for r in block[key]]
    auction = result.get("auction", {})
    if isinstance(auction.get("excluded_unlimited"), list):
        auction["excluded_unlimited"] = [
            {key: row.get(key) for key in ("symbol", "name", "main_plate", "auction_pct",
                                           "close_pct", "auction_state")}
            for row in auction["excluded_unlimited"]]
    return result


def _selection_label(abs_rank: int | None, amount_rank: int | None) -> str:
    if abs_rank is not None and amount_rank is not None:
        return f"绝对值#{abs_rank}/额#{amount_rank}"
    if abs_rank is not None:
        return f"绝对值#{abs_rank}"
    return f"额#{amount_rank}"


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _red_rate(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    values = [_number(row.get(field)) for row in rows]
    known = [value for value in values if value is not None]
    return {"n": len(known), "red": sum(value > 0 for value in known),
            "red_rate": sum(value > 0 for value in known) / len(known) if known else None,
            "mean_pct": _mean(known)}


def _amount_weighted_outcome(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    pairs = [(_number(row.get(field)), _number(row.get("auction_amount_yuan"))) for row in rows]
    known = [(value, amount) for value, amount in pairs
             if value is not None and amount is not None and amount >= 0]
    total = sum(amount for _, amount in known)
    return {**_red_rate(rows, field),
            "amount_weighted_pct": (sum(value * amount for value, amount in known) / total
                                    if total > 0 else None)}


def build_extremes_board(rows: Sequence[Mapping[str, Any]], *,
                         stage: str | None = None,
                         k_abs: int = DEFAULT_K_ABS,
                         k_amount: int = DEFAULT_K_AMOUNT,
                         min_auction_amount_yuan: float = DEFAULT_MIN_AUCTION_AMOUNT_YUAN,
                         threshold_pct: float = DEFAULT_DIRECTION_THRESHOLD_PCT) -> dict[str, Any]:
    """Select once using auction observations, then track only that fixed cohort."""
    if k_abs < 0 or k_amount < 0 or min_auction_amount_yuan < 0:
        raise ValueError("selection parameters must be non-negative")
    normalized = []
    unlimited = []
    eligible = []
    for source in rows:
        row = dict(source)
        row["symbol"] = _symbol(row)
        row["auction_pct"] = _number(row.get("p0925_pct", row.get("auction_pct")))
        row["auction_amount_yuan"] = _number(row.get("auction_amount_yuan", row.get("match_amount_yuan")))
        row["auction_price_status"] = row.get("auction_price_status") or "UNKNOWN"
        normalized.append(row)
        if _unlimited(row):
            row["auction_state"] = "新股无涨跌停限制"
            unlimited.append(row)
        elif row["auction_pct"] is not None and row["auction_amount_yuan"] is not None and row["auction_amount_yuan"] >= min_auction_amount_yuan:
            eligible.append(row)
    by_abs = sorted((row for row in eligible if row["auction_pct"] is not None),
                    key=lambda row: (-abs(row["auction_pct"]), row["symbol"]))[:k_abs]
    by_amount = sorted(eligible, key=lambda row: (-row["auction_amount_yuan"], row["symbol"]))[:k_amount]
    abs_ranks = {row["symbol"]: i for i, row in enumerate(by_abs, 1)}
    amount_ranks = {row["symbol"]: i for i, row in enumerate(by_amount, 1)}
    selected_symbols = set(abs_ranks) | set(amount_ranks)
    selected = [row for row in eligible if row["symbol"] in selected_symbols]
    selected.sort(key=lambda row: (min(abs_ranks.get(row["symbol"], 10**9),
                                      amount_ranks.get(row["symbol"], 10**9)), row["symbol"]))
    for row in selected:
        row["abs_rank"] = abs_ranks.get(row["symbol"])
        row["amount_rank"] = amount_ranks.get(row["symbol"])
        row["selection_reason"] = _selection_label(row["abs_rank"], row["amount_rank"])
        limit = _number(row.get("limit_pct"))
        pct = row["auction_pct"]
        lock = row.get("auction_limit_state")
        row["auction_state"] = ("新股无涨跌停限制" if _unlimited(row) else
                                "竞价涨停价/买不到" if lock in (1, "1", "UP") or (limit and pct >= limit - .2) else
                                "竞价跌停价" if lock in (-1, "-1", "DOWN") or (limit and pct <= -limit + .2) else
                                "INDICATIVE" if row["auction_price_status"] == "INDICATIVE" else "")
    amount_symbols = {row["symbol"] for row in by_amount}
    amount_cohort = [row for row in selected if row["symbol"] in amount_symbols]
    auction_rows = [{**row, "tracking": None} for row in selected]
    traced = [{**row, "tracking": _track(row)} for row in selected]
    endpoint = "close" if stage == "close" else "latest" if stage == "open" else None
    outcome = {}
    if endpoint:
        ranked = [row for row in traced if row["tracking"][f"delta_{endpoint}_pp"] is not None]
        ranked.sort(key=lambda row: (-row["tracking"][f"delta_{endpoint}_pp"], row["symbol"]))
        rebounds = ranked[:8]
        weakened = list(reversed(ranked[-8:]))
        high = [row for row in selected if row["auction_pct"] is not None and row["auction_pct"] >= threshold_pct]
        low = [row for row in selected if row["auction_pct"] is not None and row["auction_pct"] <= -threshold_pct]
        outcome = {
            "endpoint": endpoint,
            "rebound_top8": rebounds,
            "weak_top8": weakened,
            "high_open_group": _red_rate(high, "p0945_pct" if endpoint == "latest" else "close_pct"),
            "low_open_group": _red_rate(low, "p0945_pct" if endpoint == "latest" else "close_pct"),
            "amount_top20": _amount_weighted_outcome(
                amount_cohort, "p0945_pct" if endpoint == "latest" else "close_pct"),
            "missing_close_count": sum(row["tracking"]["close_pct"] is None for row in traced),
            "sample_label": "当日描述",
        }
    result = {
        "schema_version": 1,
        "parameters": {"k_abs": k_abs, "k_amount": k_amount,
                       "min_auction_amount_yuan": min_auction_amount_yuan,
                       "threshold_pct": threshold_pct},
        "selection_cutoff": "<=09:25; post-auction fields are not used for selection",
        "rank_legend": "绝对值#=竞价涨跌幅绝对值排名；额#=竞价匹配额排名。",
        "auction": {"rows": auction_rows, "selected_count": len(selected),
                    "excluded_unlimited": unlimited, "amount_cohort": [r for r in auction_rows if r["symbol"] in amount_symbols]},
        "open": {"rows": traced, "selected_count": len(selected), **outcome} if stage == "open" else {
            "rows": traced, "selected_count": len(selected), "endpoint": "latest",
            "rebound_top8": [], "weak_top8": [], "high_open_group": _red_rate([], "p0945_pct"),
            "low_open_group": _red_rate([], "p0945_pct"), "amount_top20": _amount_weighted_outcome([], "p0945_pct"),
            "missing_close_count": sum(row["tracking"]["close_pct"] is None for row in traced), "sample_label": "当日描述"},
        "close": {"rows": traced, "selected_count": len(selected), **outcome} if stage == "close" else {
            "rows": traced, "selected_count": len(selected), "endpoint": "close",
            "rebound_top8": [], "weak_top8": [], "high_open_group": _red_rate([], "close_pct"),
            "low_open_group": _red_rate([], "close_pct"), "amount_top20": _amount_weighted_outcome([], "close_pct"),
            "missing_close_count": sum(row["tracking"]["close_pct"] is None for row in traced), "sample_label": "当日描述"},
    }
    return _slim_result(result, stage)

