"""Standalone, post-close auction-to-close extremes review.

All percentages are percentage points. Missing inputs stay missing; they are
never converted to zero. The module is deliberately separate from live reports.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence

from .daily_kline_capture import load_capture

CONTRACT = "ExtremesReviewV1"

# Regime thresholds are explicit and intentionally easy to recalibrate.
PANIC_LEFT_TAIL_MIN = 300
RECOVERY_RIGHT_TAIL_MIN = 200
WEAK_UP_RATE_MAX = .25
WEAK_MEDIAN_MAX = -1.2
REGIME_MIN_SAMPLE_DAYS = 3


def _num(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def limit_percent(symbol: str, name: str = "") -> float | None:
    code = str(symbol or "").upper()
    if code.startswith(("N", "C")) or str(name or "").upper().startswith(("N", "C")):
        return None
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("4", "8", "92")):
        return 30.0
    return 10.0 if code[:1].isdigit() else None


def normalize_extreme(delta_pp: float | None, symbol: str, name: str = "") -> float | None:
    band = limit_percent(symbol, name)
    return None if delta_pp is None or band is None else delta_pp / band


def _pct(row: Mapping[str, Any], field: str, pre_close: float | None) -> float | None:
    direct = _num(row.get(field))
    if direct is not None:
        return direct
    close = _num(row.get("close" if field == "close_pct" else "open"))
    return (close / pre_close - 1) * 100 if close is not None and pre_close else None


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _mean(values: Sequence[float]) -> float | None:
    return statistics.mean(values) if values else None


def _quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _rate(n: int, d: int) -> float | None:
    return n / d if d else None


def classify_regime(*, left_tail: int | None, right_tail: int | None, market_median: float | None,
                    up_rate: float | None) -> str:
    if left_tail is None or right_tail is None:
        return "UNKNOWN_INSUFFICIENT_DELTA"
    if left_tail >= PANIC_LEFT_TAIL_MIN:
        return "PANIC"
    if right_tail >= RECOVERY_RIGHT_TAIL_MIN and market_median is not None and market_median > 0:
        return "RECOVERY_STRONG"
    if (up_rate is not None and up_rate <= WEAK_UP_RATE_MAX) or (market_median is not None and market_median <= WEAK_MEDIAN_MAX):
        return "WEAK"
    return "NORMAL"


REGIME_LABELS = {"PANIC": "恐慌日", "RECOVERY_STRONG": "修复/强势日", "WEAK": "弱势日", "NORMAL": "常态日"}


def _prof(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    vals = [r["delta_over_limit"] for r in rows if r.get("delta_over_limit") is not None]
    closes = [r["close_pct"] for r in rows if r.get("close_pct") is not None]
    return {
        "n": len(vals), "delta_over_limit_mean": _mean(vals), "delta_over_limit_median": _median(vals),
        "big_meat_rate": _rate(sum(x >= .5 for x in vals), len(vals)),
        "big_loss_rate": _rate(sum(x <= -.5 for x in vals), len(vals)),
        "close_green_rate": _rate(sum(x > 0 for x in closes), len(closes)),
    }


def _path_test(row: Mapping[str, Any], upper: float, lower: float) -> dict[str, bool]:
    hi, lo, close = row.get("high_pct"), row.get("low_pct"), row.get("close_pct")
    hit_up = hi is not None and hi >= upper - .2
    hit_down = lo is not None and lo <= -(lower - .2)
    up_close = close is not None and close >= upper - .2
    down_close = close is not None and close <= -(lower - .2)
    return {"hit_up": hit_up, "hit_down": hit_down, "up_close": up_close, "down_close": down_close}


def _bucket(value: float | None, edges: Sequence[tuple[str, float | None, float | None]]) -> str | None:
    if value is None:
        return None
    for label, low, high in edges:
        if (low is None or value >= low) and (high is None or value < high):
            return label
    return edges[-1][0]


def _groups(rows: Sequence[Mapping[str, Any]], key: str, edges: Sequence[tuple[str, float | None, float | None]]) -> dict[str, Any]:
    out = {}
    for row in rows:
        label = _bucket(row.get(key), edges)
        if label:
            out.setdefault(label, []).append(row)
    return {label: _prof(group) for label, group in out.items()}


def derive_extremes_review(rows: Sequence[Mapping[str, Any]], *, trade_date: str,
                           plates: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    stocks = []
    excluded_new = 0
    for source in rows:
        row = dict(source)
        symbol = str(row.get("symbol") or "")
        row["limit_pct"] = _num(row.get("limit_pct")) or limit_percent(symbol, str(row.get("name") or ""))
        if row["limit_pct"] is None:
            excluded_new += 1
            continue
        for f in ("auction_pct", "open_pct", "p0932_pct", "p0945_pct", "close_pct", "high_pct", "low_pct"):
            row[f] = _num(row.get(f))
        if row.get("delta_pp") is None and row.get("auction_pct") is not None and row.get("close_pct") is not None:
            row["delta_pp"] = row["close_pct"] - row["auction_pct"]
        else:
            row["delta_pp"] = _num(row.get("delta_pp"))
        row["delta_over_limit"] = normalize_extreme(row["delta_pp"], symbol, str(row.get("name") or ""))
        row["is_big_meat"] = row["delta_over_limit"] is not None and row["delta_over_limit"] >= .5
        row["is_big_loss"] = row["delta_over_limit"] is not None and row["delta_over_limit"] <= -.5
        row["path_flags"] = _path_test(row, row["limit_pct"], row["limit_pct"])
        stocks.append(row)

    close = [r["close_pct"] for r in stocks if r["close_pct"] is not None]
    delta = [r["delta_pp"] for r in stocks if r["delta_pp"] is not None]
    scaled = [r["delta_over_limit"] for r in stocks if r["delta_over_limit"] is not None]
    left, right = [r for r in stocks if r.get("delta_pp") is not None and r["delta_pp"] <= -8], [r for r in stocks if r.get("delta_pp") is not None and r["delta_pp"] >= 8]
    left_scaled, right_scaled = [r for r in stocks if r.get("delta_over_limit") is not None and r["delta_over_limit"] <= -.5], [r for r in stocks if r.get("delta_over_limit") is not None and r["delta_over_limit"] >= .5]
    pool = [r for r in stocks if r.get("in_yest_pool")]
    paths: dict[str, list[dict[str, Any]]] = {k: [] for k in (
        "地天", "近地天", "天地", "近天地", "竞价封跌", "竞价封涨收盘未封",
        "连板>=3昨日今日断板", "连续跌停后开板", "一字涨停", "一字跌停")}
    for r in stocks:
        f = r["path_flags"]
        if f["hit_down"] and f["up_close"]: paths["地天"].append(r)
        if f["hit_down"] and r.get("close_pct") is not None and r["close_pct"] > 0 and not f["up_close"]: paths["近地天"].append(r)
        if f["hit_up"] and f["down_close"]: paths["天地"].append(r)
        if f["hit_up"] and r.get("close_pct") is not None and r["close_pct"] < 0 and not f["down_close"]: paths["近天地"].append(r)
        if r.get("auction_limit_state") == -1: paths["竞价封跌"].append(r)
        if r.get("auction_limit_state") == 1 and not f["up_close"]: paths["竞价封涨收盘未封"].append(r)
        if (r.get("prior_limit_streak") or r.get("lb_days") or 0) >= 3 and not f["up_close"]: paths["连板>=3昨日今日断板"].append(r)
        if (r.get("prior_down_streak") or 0) >= 2 and not f["down_close"]: paths["连续跌停后开板"].append(r)
        if f["up_close"] and r.get("high_pct") is not None and r["high_pct"] <= r["limit_pct"] + .001: paths["一字涨停"].append(r)
        if f["down_close"] and r.get("low_pct") is not None and r["low_pct"] >= -(r["limit_pct"] + .001): paths["一字跌停"].append(r)

    auction_edges = [("<=-3", None, -3), ("-3~-1", -3, -1), ("-1~+1", -1, 1), ("+1~+3", 1, 3), ("+3~+6", 3, 6), (">=+6", 6, None)]
    rank_edges = [("前100", 1, 101), ("100-500", 101, 501), ("500-1500", 501, 1501), ("1500-3000", 1501, 3001), (">3000", 3001, None)]
    rel_edges = [("<=-2", None, -2), ("-2~+2", -2, 2)]
    # Compute the observed last-tick move from 09:24 to 09:25 when both are present.
    for row in stocks:
        p24, p25 = row.get("p0924_pct"), row.get("p0925_pct") if row.get("p0925_pct") is not None else row.get("auction_pct")
        row["move_0924_0925_pp"] = p25 - p24 if p24 is not None and p25 is not None else _num(row.get("move_0924_0925_pp"))
    move_values = [r for r in stocks if r.get("move_0924_0925_pp") is not None]
    binned = {"auction_pct": _groups(stocks, "auction_pct", auction_edges),
              "auction_amount_rank": _groups(stocks, "auction_amount_rank", rank_edges),
              "rel_to_strongest_plate": _groups(stocks, "rel_to_strongest_plate", rel_edges),
              "move_0924_0925_pp": {"<=-2": _prof([r for r in move_values if r["move_0924_0925_pp"] <= -2]),
                                    "-2~+2": _prof([r for r in move_values if -2 < r["move_0924_0925_pp"] < 2]),
                                    ">=+2": _prof([r for r in move_values if r["move_0924_0925_pp"] >= 2])}}
    rel_values = [r for r in stocks if r.get("rel_to_strongest_plate") is not None]
    binned["rel_to_strongest_plate"][">=+2"] = _prof([r for r in rel_values if r["rel_to_strongest_plate"] >= 2])
    binned["rel_to_strongest_plate"][">=+4"] = _prof([r for r in rel_values if r["rel_to_strongest_plate"] >= 4])
    for label, group in (("yest_pool", pool), ("not_yest_pool", [r for r in stocks if not r.get("in_yest_pool")]),
                         ("yest_pool_auction_ge_2", [r for r in pool if r.get("auction_pct") is not None and r["auction_pct"] >= 2])):
        binned[label] = _prof(group)
    baseline = _prof(stocks)
    rel_group = [r for r in stocks if r.get("rel_to_strongest_plate") is not None and r["rel_to_strongest_plate"] >= 2]
    low_group = [r for r in stocks if r.get("auction_pct") is not None and r["auction_pct"] <= -3]

    plate_rows = []
    for plate in plates:
        members = [r for r in stocks if plate.get("name") in (r.get("plates") or [])]
        if len(members) < 8 or plate.get("generic"):
            continue
        pp = _prof(members)
        auc = [r["auction_pct"] for r in members if r.get("auction_pct") is not None]
        cls = [r["close_pct"] for r in members if r.get("close_pct") is not None]
        plate_rows.append({"code": plate.get("code"), "name": plate.get("name"), "members": len(members),
                           **pp, "auction_pct_mean": _mean(auc), "close_pct_mean": _mean(cls),
                           "big_meat_count": sum(bool(r["is_big_meat"]) for r in members),
                           "big_loss_count": sum(bool(r["is_big_loss"]) for r in members)})
    plate_rows.sort(key=lambda r: (r["delta_over_limit_mean"] if r["delta_over_limit_mean"] is not None else -math.inf, r["name"] or ""), reverse=True)
    strongest = sorted(plate_rows, key=lambda r: (r["auction_pct_mean"] if r["auction_pct_mean"] is not None else -math.inf), reverse=True)[:10]
    richest = sorted(plate_rows, key=lambda r: (r["delta_over_limit_mean"] if r["delta_over_limit_mean"] is not None else -math.inf), reverse=True)[:10]
    poorest = sorted(plate_rows, key=lambda r: (r["delta_over_limit_mean"] if r["delta_over_limit_mean"] is not None else math.inf))[:10]
    delta_known = bool(delta)
    regime = classify_regime(left_tail=len(left) if delta_known else None, right_tail=len(right) if delta_known else None, market_median=_median(close),
                             up_rate=_rate(sum(x > 0 for x in close), len(close)))
    return {
        "contract": CONTRACT, "trade_date": trade_date, "stocks": stocks,
        "regime": regime,
        "summary": {"n": len(stocks), "close_median": _median(close), "close_mean": _mean(close),
                    "close_up_rate": _rate(sum(x > 0 for x in close), len(close)),
                    "delta_quantiles": {f"p{p}": _quantile(delta, q) for p, q in ((1,.01),(5,.05),(50,.5),(95,.95),(99,.99))},
                    "delta_over_limit_quantiles": {f"p{p}": _quantile(scaled, q) for p, q in ((1,.01),(5,.05),(50,.5),(95,.95),(99,.99))},
                    "left_tail_delta_le_minus_8": len(left) if delta_known else None, "right_tail_delta_ge_8": len(right) if delta_known else None,
                    "left_tail_scaled_le_minus_05": len(left_scaled) if scaled else None, "right_tail_scaled_ge_05": len(right_scaled) if scaled else None,
                    "close_limit_up_count": sum(r["path_flags"]["up_close"] for r in stocks),
                    "close_limit_down_count": sum(r["path_flags"]["down_close"] for r in stocks),
                    "one_word_limit_up_count": sum(r["path_flags"]["up_close"] and r.get("open_pct") is not None and r.get("high_pct") is not None and abs(r["open_pct"] - r["close_pct"]) < .001 and abs(r["high_pct"] - r["close_pct"]) < .001 for r in stocks),
                    "one_word_limit_down_count": sum(r["path_flags"]["down_close"] and r.get("open_pct") is not None and r.get("low_pct") is not None and abs(r["open_pct"] - r["close_pct"]) < .001 and abs(r["low_pct"] - r["close_pct"]) < .001 for r in stocks),
                    "indicative_count": sum(r.get("auction_price_status") == "INDICATIVE" for r in stocks),
                    "excluded_new_listing_count": excluded_new, "yest_pool": _prof(pool),
                    "yest_pool_break_count": sum((r.get("lb_days") or r.get("prior_limit_streak") or 0) > 0 and not r["path_flags"]["up_close"] for r in pool)},
        "big_meat": sorted([r for r in stocks if r["is_big_meat"]], key=lambda r: (-r["delta_over_limit"], r["symbol"]))[:30],
        "big_loss": sorted([r for r in stocks if r["is_big_loss"]], key=lambda r: (r["delta_over_limit"], r["symbol"]))[:30],
        "paths": paths, "bins": binned,
        "diagnostics": {"rel_ge_2_big_loss_rate": _rate(sum(r["delta_over_limit"] is not None and r["delta_over_limit"] <= -.5 for r in rel_group), len(rel_group)),
                        "rel_ge_2_n": len(rel_group), "baseline_big_loss_rate": baseline["big_loss_rate"],
                        "auction_le_minus_3_big_meat_rate": _rate(sum(r["delta_over_limit"] is not None and r["delta_over_limit"] >= .5 for r in low_group), len(low_group)),
                        "auction_le_minus_3_n": len(low_group), "baseline_big_meat_rate": baseline["big_meat_rate"],
                        "four_day_clue_only": True},
        "plates": {"availability": "READY" if plates else "UNAVAILABLE_MISSING_RELATION_DIR",
                   "best_delta_over_limit": richest, "worst_delta_over_limit": poorest,
                   "strongest_auction": strongest,
                   "strongest_close_below_auction_count": sum((r["close_pct_mean"] or 0) < (r["auction_pct_mean"] or 0) for r in strongest)},
    }


def compare_history(today: Mapping[str, Any], history_dir: str | Path | None) -> dict[str, Any]:
    past = []
    if history_dir and Path(history_dir).is_dir():
        for path in sorted(Path(history_dir).glob("????-??-??.json")):
            if path.stem == today.get("trade_date"):
                continue
            try: past.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError): continue
    all_rows = past + [dict(today)]
    rows = []
    for item in all_rows:
        s = item.get("summary", {}); d = item.get("diagnostics", {}); p = item.get("plates", {})
        regime = item.get("regime") or classify_regime(
            left_tail=s.get("left_tail_delta_le_minus_8"), right_tail=s.get("right_tail_delta_ge_8"),
            market_median=s.get("close_median"), up_rate=s.get("close_up_rate"))
        rows.append({"trade_date": item.get("trade_date"), "market_median": s.get("close_median"),
                     "regime": regime,
                     "up_rate": s.get("close_up_rate"), "delta_median": s.get("delta_quantiles", {}).get("p50"),
                     "delta_p5": s.get("delta_quantiles", {}).get("p5"), "delta_p95": s.get("delta_quantiles", {}).get("p95"),
                     "left_tail_count": s.get("left_tail_delta_le_minus_8"), "right_tail_count": s.get("right_tail_delta_ge_8"),
                     "big_meat_count": sum(r.get("is_big_meat", False) for r in item.get("stocks", [])),
                     "big_loss_count": sum(r.get("is_big_loss", False) for r in item.get("stocks", [])),
                     "yest_pool_big_loss_rate": item.get("bins", {}).get("yest_pool", {}).get("big_loss_rate"),
                     "rel_big_loss_rate": d.get("rel_ge_2_big_loss_rate"), "baseline_big_loss_rate": d.get("baseline_big_loss_rate"),
                     "deep_low_big_meat_rate": d.get("auction_le_minus_3_big_meat_rate"), "baseline_big_meat_rate": d.get("baseline_big_meat_rate"),
                     "strongest_plates_mean_delta_l": _mean([x["delta_over_limit_mean"] for x in p.get("strongest_auction", []) if x.get("delta_over_limit_mean") is not None]),
                     "strongest_plates_close_below_auction": p.get("strongest_close_below_auction_count")})
    today_row = next(r for r in rows if r["trade_date"] == today.get("trade_date"))
    value = today_row["left_tail_count"]
    population = [r["left_tail_count"] for r in rows if r["left_tail_count"] is not None]
    today_row["left_tail_historical_percentile"] = _rate(sum(x <= value for x in population), len(population)) if value is not None else None
    same = [r for r in rows if r["regime"] == today_row["regime"] and r["trade_date"] != today.get("trade_date")]
    return {"sample_days": len(rows), "rows": rows, "today_regime": today_row["regime"],
            "same_regime_days": len(same), "same_regime_market_median": _median([r["market_median"] for r in same if r["market_median"] is not None]),
            "same_regime_big_loss_rate": _mean([r["baseline_big_loss_rate"] for r in same if r.get("baseline_big_loss_rate") is not None]),
            "today_left_tail_historical_percentile": today_row["left_tail_historical_percentile"]}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _daily_profile(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    valid = [r for r in rows if not r.get("excluded_new_listing") and not r.get("missing_price")]
    returns = [r["close_pct"] for r in valid if r.get("close_pct") is not None]
    deltas = [r["delta_pp"] for r in valid if r.get("delta_pp") is not None]
    left = sum(x <= -8 for x in deltas) if deltas else None
    right = sum(x >= 8 for x in deltas) if deltas else None
    up_limit = sum(bool(r.get("path_flags", {}).get("up_close")) for r in valid)
    down_limit = sum(bool(r.get("path_flags", {}).get("down_close")) for r in valid)
    return {
        "n": len(returns), "market_median": _median(returns), "up_rate": _rate(sum(x > 0 for x in returns), len(returns)),
        "delta_median": _median(deltas), "delta_p5": _quantile(deltas, .05), "delta_p95": _quantile(deltas, .95),
        "left_tail_count": left, "right_tail_count": right, "limit_up_count": up_limit, "limit_down_count": down_limit,
        "big_meat_count": sum(r.get("delta_over_limit") is not None and r["delta_over_limit"] >= .5 for r in valid),
        "big_loss_count": sum(r.get("delta_over_limit") is not None and r["delta_over_limit"] <= -.5 for r in valid),
        "excluded_new_listing_count": sum(r.get("excluded_new_listing") is True for r in rows),
        "missing_price_count": sum(not r.get("excluded_new_listing") and r.get("missing_price") is True for r in rows),
        "source_mode": "DAILY_ONLY",
    }


def _profile_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    vals = [r for r in rows if r.get("delta_over_limit") is not None]
    opens = [r["open_pct"] for r in rows if r.get("open_pct") is not None]
    closes = [r["close_pct"] for r in rows if r.get("close_pct") is not None]
    return {"n": len(vals), "open_premium_mean": _mean(opens), "close_mean": _mean(closes),
            "delta_over_limit_mean": _mean([r["delta_over_limit"] for r in vals]),
            "big_meat_rate": _rate(sum(r["delta_over_limit"] >= .5 for r in vals), len(vals)),
            "big_loss_rate": _rate(sum(r["delta_over_limit"] <= -.5 for r in vals), len(vals)),
            "break_rate": _rate(sum(not r["path_flags"]["up_close"] for r in rows if r.get("prior_limit_streak", 0) > 0),
                                 sum(r.get("prior_limit_streak", 0) > 0 for r in rows))}


def _daily_regime_tables(days: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_regime: dict[str, list[Mapping[str, Any]]] = {}
    for day in days:
        by_regime.setdefault(str(day["regime"]), []).append(day)
    result = {}
    for regime, regime_days in by_regime.items():
        stocks = [r for day in regime_days for r in day["stocks"] if r.get("delta_over_limit") is not None]
        base = _profile_rows(stocks)
        def sufficient(stats: dict[str, Any]) -> dict[str, Any]:
            return {**stats, "sample_days": len(regime_days),
                    "status": "READY" if len(regime_days) >= REGIME_MIN_SAMPLE_DAYS else "INSUFFICIENT_SAMPLE"}
        open_buckets = {
            "open_le_minus_3": sufficient(_profile_rows([r for r in stocks if r.get("open_pct") is not None and r["open_pct"] <= -3])),
            "open_ge_plus_3": sufficient(_profile_rows([r for r in stocks if r.get("open_pct") is not None and r["open_pct"] >= 3])),
            "market_baseline": sufficient(base),
        }
        position_groups: dict[str, list[Mapping[str, Any]]] = {f"Q{i}": [] for i in range(1, 6)}
        for day in regime_days:
            for r in day["stocks"]:
                q = r.get("prior_range_quintile")
                if q and r.get("delta_over_limit") is not None:
                    position_groups[q].append(r)
        position = {q: sufficient({**_profile_rows(group), "close_median": _median([r["close_pct"] for r in group if r.get("close_pct") is not None])})
                    for q, group in position_groups.items()}
        yest_pool = [r for r in stocks if r.get("in_yest_pool")]
        three_plus = [r for r in stocks if (r.get("prior_limit_streak") or 0) >= 3]
        result[regime] = {"sample_days": len(regime_days), "sample_stocks": len(stocks),
                          "status": "READY" if len(regime_days) >= REGIME_MIN_SAMPLE_DAYS else "INSUFFICIENT_SAMPLE",
                          "open_buckets": open_buckets, "prior_close_position_quintiles": position,
                          "yest_limit_pool": sufficient(_profile_rows(yest_pool)),
                          "prior_three_plus_limit": sufficient(_profile_rows(three_plus))}
    return result


def build_daily_history(path: str | Path) -> dict[str, Any]:
    """Build dated, daily-only market summaries and regime-conditioned facts."""
    source = _read_csv(Path(path))
    raw_by_code: dict[str, list[dict[str, Any]]] = {}
    for source_row in source:
        code = _symbol(source_row.get("symbol", source_row.get("code")))
        item = {**source_row, "symbol": code, "date": str(source_row.get("date") or "")}
        raw_by_code.setdefault(code, []).append(item)
    normalized_by_day: dict[str, list[dict[str, Any]]] = {}
    codes_by_day: dict[str, int] = {}
    for code, items in raw_by_code.items():
        ordered = sorted(items, key=lambda r: r["date"])
        prior_closes: list[float] = []
        for index, raw in enumerate(ordered):
            day = raw["date"]
            name = str(raw.get("name") or "")
            band = limit_percent(code, name)
            is_nc = band is None
            op, hi, lo, close = (_num(raw.get(k)) for k in ("open", "high", "low", "close"))
            pre = _num(raw.get("pre_close")) or (prior_closes[-1] if prior_closes else None)
            close_pct = ((close / pre - 1) * 100) if close is not None and pre else _num(raw.get("pct_chg"))
            open_pct = ((op / pre - 1) * 100) if op is not None and pre else None
            high_pct = ((hi / pre - 1) * 100) if hi is not None and pre else None
            low_pct = ((lo / pre - 1) * 100) if lo is not None and pre else None
            missing_price = close is None
            row: dict[str, Any] = {"symbol": code, "name": name, "limit_pct": band, "open_pct": open_pct,
                                   "close_pct": close_pct, "high_pct": high_pct, "low_pct": low_pct,
                                   "excluded_new_listing": is_nc, "missing_price": missing_price,
                                   "in_yest_pool": False, "prior_limit_streak": 0,
                                   "delta_pp": close_pct - open_pct if close_pct is not None and open_pct is not None else None}
            if not is_nc and not missing_price and band is not None:
                row["path_flags"] = _path_test(row, band, band)
                row["delta_over_limit"] = normalize_extreme(row["delta_pp"], code, name)
                if index:
                    prev_close = _num(ordered[index - 1].get("close"))
                    prev_pre = _num(ordered[index - 1].get("pre_close")) or (_num(ordered[index - 2].get("close")) if index > 1 else None)
                    row["in_yest_pool"] = bool(prev_close is not None and prev_pre and (prev_close / prev_pre - 1) * 100 >= band - .2)
                streak = 0
                for previous_index in range(index - 1, -1, -1):
                    previous = ordered[previous_index]
                    pclose = _num(previous.get("close")); ppre = _num(previous.get("pre_close")) or (_num(ordered[previous_index - 1].get("close")) if previous_index > 0 else None)
                    if pclose is None or ppre is None or (pclose / ppre - 1) * 100 < band - .2: break
                    streak += 1
                row["prior_limit_streak"] = streak
                if index >= 20:
                    closes20 = [_num(x.get("close")) for x in ordered[index-20:index]]
                    closes20 = [x for x in closes20 if x is not None]
                    if len(closes20) == 20 and max(closes20) > min(closes20):
                        position = (close - min(closes20)) / (max(closes20) - min(closes20))
                        row["prior_range_quintile"] = f"Q{min(5, max(1, int(position * 5) + 1))}"
            else:
                row["delta_over_limit"] = None
            normalized_by_day.setdefault(day, []).append(row)
            codes_by_day[day] = codes_by_day.get(day, 0) + 1
            if close is not None:
                prior_closes.append(close)
    days = []
    for day in sorted(normalized_by_day):
        stocks = normalized_by_day[day]
        valid = [r for r in stocks if not r["excluded_new_listing"] and not r["missing_price"] and r.get("close_pct") is not None]
        summary = _daily_profile(stocks)
        regime = classify_regime(left_tail=summary["left_tail_count"], right_tail=summary["right_tail_count"],
                                 market_median=summary["market_median"], up_rate=summary["up_rate"])
        yest = [r for r in valid if r.get("in_yest_pool")]
        panic_down = [r for r in valid if r.get("close_pct") is not None and r["close_pct"] <= -(r["limit_pct"] - .2)]
        summary.update({"valid_n": len(valid), "yest_pool_close_mean": _mean([r["close_pct"] for r in yest]),
                        "yest_pool_big_loss_rate": _rate(sum((r.get("delta_over_limit") or 0) <= -.5 for r in yest), len(yest)),
                        "yest_pool_n": len(yest), "td_pool_n": None, "daily_csv_rows": codes_by_day[day],
                        "extra_code_count": None, "panic_close_down_count": len(panic_down),
                        "hit_down_count": sum(r.get("path_flags", {}).get("hit_down", False) for r in valid),
                        "hit_down_close_green_rate": _rate(sum(r.get("path_flags", {}).get("hit_down", False) and r.get("close_pct", 0) > 0 for r in valid),
                                                            sum(r.get("path_flags", {}).get("hit_down", False) for r in valid))})
        paths = {"地天": 0, "近地天": 0, "天地": 0}
        for row in valid:
            flags = row.get("path_flags", {})
            if flags.get("hit_down") and flags.get("up_close"): paths["地天"] += 1
            if flags.get("hit_down") and row.get("close_pct", 0) > 0 and not flags.get("up_close"): paths["近地天"] += 1
            if flags.get("hit_up") and flags.get("down_close"): paths["天地"] += 1
        days.append({"trade_date": day, "regime": regime, "summary": summary, "stocks": stocks, "paths": paths})
    # T-session close-down limit cohort performance through T+3.
    by_date = {d["trade_date"]: d for d in days}
    panic_sequences = []
    for i, day in enumerate(days):
        if day["regime"] != "PANIC":
            continue
        panic_stocks = [r for r in day["stocks"] if not r["excluded_new_listing"] and not r["missing_price"] and
                        r.get("close_pct") is not None and r["close_pct"] <= -(r["limit_pct"] - .2)]
        sequence = {"panic_date": day["trade_date"], "sessions": []}
        for offset in range(4):
            if i + offset >= len(days):
                sequence["sessions"].append({"offset": offset, "trade_date": None, "status": "NO_FOLLOWING_SESSION"})
                continue
            next_day = days[i + offset]
            profile = next_day["summary"].copy()
            follow = {r["symbol"]: r for r in next_day["stocks"]}
            cohort = [follow[r["symbol"]] for r in panic_stocks if r["symbol"] in follow and not follow[r["symbol"]]["missing_price"]]
            profile.update({"panic_down_cohort_n": len(cohort),
                            "panic_down_open_mean": _mean([r["open_pct"] for r in cohort if r.get("open_pct") is not None]),
                            "panic_down_close_mean": _mean([r["close_pct"] for r in cohort if r.get("close_pct") is not None]),
                            "panic_down_close_green_rate": _rate(sum(r.get("close_pct") is not None and r["close_pct"] > 0 for r in cohort), len(cohort)),
                            "panic_down_limit_again_count": sum(r.get("path_flags", {}).get("down_close", False) for r in cohort)})
            sequence["sessions"].append({"offset": offset, "trade_date": next_day["trade_date"], "regime": next_day["regime"], "summary": profile})
        panic_sequences.append(sequence)
    return {"mode": "DAILY_ONLY", "source_file": str(path), "days": days,
            "regime_tables": _daily_regime_tables(days), "panic_sequences": panic_sequences,
            "daily_only_columns": ["open-vs-close daily facts", "regime-conditioned groups", "panic follow-through"],
            "unavailable_intraday_columns": ["rel_to_strongest_plate", "auction amount/rank", "move_0924_0925_pp", "p0932_pct", "p0945_pct"]}


def _symbol(value: Any) -> str:
    text = str(value or "").strip()
    return text.rsplit(".", 1)[-1].zfill(6) if text.isdigit() else text


def _prior_streaks(rows: Sequence[Mapping[str, Any]], symbol: str, name: str = "") -> tuple[int, int]:
    ordered = sorted(rows, key=lambda row: str(row.get("date") or ""))
    up = down = 0
    band = limit_percent(symbol, name)
    if band is None:
        return 0, 0
    for row in reversed(ordered):
        pc, close = _num(row.get("pre_close")), _num(row.get("close"))
        if not pc or close is None or (close / pc - 1) * 100 < band - .2:
            break
        up += 1
    for row in reversed(ordered):
        pc, close = _num(row.get("pre_close")), _num(row.get("close"))
        if not pc or close is None or (close / pc - 1) * 100 > -(band - .2):
            break
        down += 1
    return up, down


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def assemble_from_files(trade_date: str, report_dir: str | Path, checkpoints_csv: str | Path,
                        auction_snapshot_csv: str | Path, daily_capture_dir: str | Path,
                        daily_history_csv: str | Path | None = None, relation_dir: str | Path | None = None,
                        stock_caps_json: str | Path | None = None, generic_names_json: str | Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    report_path = Path(report_dir)
    stocks_doc = _json(report_path / "stocks.json")
    report_stocks = stocks_doc.get("stocks", stocks_doc) if isinstance(stocks_doc, dict) else stocks_doc
    stocks_by_symbol = {_symbol(r.get("symbol", r.get("code"))): dict(r) for r in report_stocks}
    check_rows = _read_csv(Path(checkpoints_csv)); auction_rows = _read_csv(Path(auction_snapshot_csv))
    checks: dict[str, dict[str, dict[str, str]]] = {}
    for row in check_rows:
        if row.get("date") == trade_date:
            checks.setdefault(_symbol(row.get("symbol")), {})[str(row.get("tag"))] = row
    auctions = {_symbol(r.get("symbol")): r for r in auction_rows if str(r.get("trade_date")) == trade_date and str(r.get("auction_tag")) == "0925"}
    daily = load_capture(daily_capture_dir)
    names = {}
    for r in report_stocks:
        names[_symbol(r.get("symbol", r.get("code")))] = r
    relation: dict[str, list[str]] = {}
    plate_meta: dict[str, dict[str, Any]] = {}
    if relation_dir:
        folder = Path(relation_dir)
        for r in _read_csv(folder / "relation.csv"):
            relation.setdefault(_symbol(r.get("stockid")), []).append(str(r.get("plateid")))
        registry = _json(folder / "registry.json")
        entries = registry.get("plates", registry) if isinstance(registry, dict) else registry
        if isinstance(entries, dict): entries = [dict(v, code=k) for k, v in entries.items()]
        plate_meta = {str(r.get("code", r.get("plateid"))): {"code": str(r.get("code", r.get("plateid"))), "name": r.get("name"), "generic": r.get("class") == "GENERIC"} for r in entries}
    generic_path = Path(generic_names_json) if generic_names_json else Path(__file__).resolve().parents[2] / "examples" / "data" / "plate_generic_factor_names.json"
    if generic_path.is_file():
        generic_doc = _json(generic_path)
        generic_names = set(generic_doc.get("names", ())) if isinstance(generic_doc, dict) else set(generic_doc)
        for plate in plate_meta.values():
            if plate.get("name") in generic_names: plate["generic"] = True
    caps = _json(Path(stock_caps_json)) if stock_caps_json else {}
    rows = []
    for symbol in sorted(set(stocks_by_symbol) | set(daily) | set(checks) | set(auctions)):
        sr = stocks_by_symbol.get(symbol, {}); d = daily.get(symbol, {}); cp = checks.get(symbol, {})
        auc = auctions.get(symbol, {})
        a_pct = _num(auc.get("chg_bp")); a_pct = a_pct / 100 if a_pct is not None else None
        c25 = cp.get("0925", {})
        if a_pct is None:
            px, pc = _num(c25.get("px")), _num(c25.get("pre_close"))
            if px is not None and pc: a_pct = (px / pc - 1) * 100
        last_ts = str(c25.get("last_tick_ts") or "")
        if last_ts.isdigit() and len(last_ts) >= 13:
            from datetime import datetime, timezone, timedelta
            time_text = datetime.fromtimestamp(int(last_ts[:13]) / 1000, timezone(timedelta(hours=8))).strftime("%H:%M:%S")
        else:
            time_text = last_ts.replace("T", " ").split(" ")[-1][:8] if last_ts else ""
        price_status = ("FINAL" if time_text >= "09:25:00" else "INDICATIVE") if time_text and a_pct is not None else "UNKNOWN"
        def point(tag: str) -> float | None:
            row = cp.get(tag, {}); px, pc = _num(row.get("px")), _num(row.get("pre_close"))
            return (px / pc - 1) * 100 if px is not None and pc else None
        pre = _num(d.get("pre_close")); close = _num(d.get("close"))
        openp = _num(d.get("open")); high = _num(d.get("high")); low = _num(d.get("low"))
        def daypct(px: float | None) -> float | None: return (px / pre - 1) * 100 if px is not None and pre else None
        l_pct = limit_percent(symbol, str(sr.get("name") or d.get("name") or ""))
        all_plate_codes = relation.get(symbol, [])
        assigned_plates = [plate_meta[c]["name"] for c in all_plate_codes if c in plate_meta and plate_meta[c].get("name")]
        main_plate = sr.get("primary_plate") or sr.get("plate") or (assigned_plates[0] if assigned_plates else None)
        name = sr.get("name") or d.get("name")
        row = {"symbol": symbol, "name": name, "main_plate": main_plate, "plates": assigned_plates,
               "limit_pct": l_pct, "auction_pct": a_pct, "auction_price_status": price_status,
               "p0924_pct": point("0924"), "p0925_pct": point("0925"), "p0932_pct": point("0932"), "p0945_pct": point("0945"), "open_pct": daypct(openp),
               "close_pct": daypct(close), "high_pct": daypct(high), "low_pct": daypct(low),
               "auction_amount_yuan": _num(auc.get("match_amt_yuan")),
               "rest_bid_yuan": _num(auc.get("rest_bid_amt_yuan")), "rest_ask_yuan": _num(auc.get("rest_ask_amt_yuan")),
               "auction_limit_state": int(_num(auc.get("limit_state")) or 0),
               "in_yest_pool": bool(sr.get("in_yest_pool", False)), "lb_days": int(_num(sr.get("lb_days")) or 0),
               "prior_limit_streak": int(_num(sr.get("lb_days")) or 0), "prior_down_streak": 0,
               "caps": caps.get(symbol)}
        rows.append(row)
    # Amount ranks and strongest-plate relative auction strength.
    amount_order = sorted((r for r in rows if r["auction_amount_yuan"] is not None), key=lambda r: (-r["auction_amount_yuan"], r["symbol"]))
    for rank, row in enumerate(amount_order, 1): row["auction_amount_rank"] = rank
    p_auction = {}
    for plate in plate_meta.values():
        members = [r["auction_pct"] for r in rows if plate.get("name") in (r.get("plates") or []) and r.get("auction_pct") is not None]
        if members: p_auction[plate["name"]] = statistics.mean(members)
    for row in rows:
        vals = [p_auction[p] for p in row["plates"] if p in p_auction]
        row["rel_to_strongest_plate"] = row["auction_pct"] - max(vals) if vals and row["auction_pct"] is not None else None
    if daily_history_csv:
        hist = _read_csv(Path(daily_history_csv)); by_code: dict[str, list[dict[str, Any]]] = {}
        for r in hist:
            code = _symbol(r.get("symbol", r.get("code"))); day = str(r.get("date") or "")
            if day < trade_date: by_code.setdefault(code, []).append(r)
        for row in rows:
            streak_up, streak_down = _prior_streaks(by_code.get(row["symbol"], []), row["symbol"], row.get("name") or "")
            row["prior_limit_streak"] = streak_up
            row["prior_down_streak"] = streak_down
    plates = list(plate_meta.values())
    td_symbols = set(stocks_by_symbol)
    daily_symbols = set(daily)
    checkpoint_symbols = set(checks)
    auction_symbols = set(auctions)
    daily_history_symbols = {_symbol(r.get("symbol", r.get("code"))) for r in _read_csv(Path(daily_history_csv))} if daily_history_csv else set()
    td_pool_rows = [r for r in rows if r["symbol"] in td_symbols]
    nc_excluded = sum(limit_percent(r["symbol"], str(r.get("name") or "")) is None for r in td_pool_rows)
    missing_auction_price = sum(r.get("auction_pct") is None for r in td_pool_rows)
    analysis_rows = [r for r in td_pool_rows if r.get("auction_pct") is not None and
                     limit_percent(r["symbol"], str(r.get("name") or "")) is not None]
    result = derive_extremes_review(analysis_rows, trade_date=trade_date, plates=plates)
    paths = [Path(report_path / n) for n in ("stocks.json", "plates.json", "plate_stock_view.json", "manifest.json") if (report_path / n).exists()]
    inputs = [Path(checkpoints_csv), Path(auction_snapshot_csv), Path(daily_capture_dir) / "daily_kline.jsonl", Path(daily_capture_dir) / "manifest.json"] + paths
    if daily_history_csv: inputs.append(Path(daily_history_csv))
    if relation_dir: inputs += [Path(relation_dir) / "relation.csv", Path(relation_dir) / "registry.json"]
    if stock_caps_json: inputs.append(Path(stock_caps_json))
    if generic_path.is_file(): inputs.append(generic_path)
    manifest = {"contract": CONTRACT, "trade_date": trade_date, "input_files": {str(p): {"sha256": _sha(p), "rows": sum(1 for _ in p.open(encoding="utf-8-sig")) - 1 if p.suffix == ".csv" else None} for p in inputs},
                "sample_composition": {"default_population": "TD_POOL_INTERSECTION_AUCTION_PRICE_NON_NC",
                    "td_pool_count": len(td_symbols), "extra_daily_or_checkpoint_code_count": len(set(r["symbol"] for r in rows) - td_symbols),
                    "daily_capture_extra_code_count": len(daily_symbols - td_symbols),
                    "checkpoint_extra_code_count": len(checkpoint_symbols - td_symbols),
                    "auction_snapshot_extra_code_count": len(auction_symbols - td_symbols),
                    "daily_history_csv_extra_code_count": len(daily_history_symbols - td_symbols) if daily_history_csv else None,
                    "td_pool_missing_auction_price_count": missing_auction_price, "td_pool_nc_excluded_count": nc_excluded,
                    "included_count": result["summary"]["n"]},
                "daily_kline_coverage_td_pool": _rate(sum(s.get("in_yest_pool") and s["symbol"] in daily for s in rows), sum(s.get("in_yest_pool") for s in rows)),
                "checkpoint_coverage": {tag: _rate(sum(tag in checks.get(s, {}) for s in stocks_by_symbol), len(stocks_by_symbol)) for tag in ("0920", "0924", "0925", "0932", "0935", "0940", "0945")},
                "nc_excluded_count": nc_excluded,
                "indicative_count": result["summary"]["indicative_count"]}
    result["manifest"] = manifest
    if daily_history_csv:
        result["daily_history"] = build_daily_history(daily_history_csv)
    return result, manifest


def render_extremes_markdown(data: Mapping[str, Any], history: Mapping[str, Any]) -> str:
    s = data["summary"]
    fmt = lambda x, digits=2: "—" if x is None else f"{x:+.{digits}f}"
    plain = lambda x, digits=2: "—" if x is None else f"{x:.{digits}f}"
    rate = lambda x, digits=1: "—" if x is None else f"{x*100:.{digits}f}%"
    regime = data.get("regime", history.get("today_regime", "UNKNOWN"))
    lines = [f"# 极值复盘 {data['trade_date']}", "", "## 市场极值总览", "",
             f"今日日型：{REGIME_LABELS.get(regime, regime)}（左尾 Δ≤−8pp {s['left_tail_delta_le_minus_8']}，右尾 Δ≥+8pp {s['right_tail_delta_ge_8']}，市场中位 {fmt(s['close_median'])}%，上涨占比 {rate(s['close_up_rate'])}）。",
             f"样本 {s['n']}；收盘中位/均值 {fmt(s['close_median'])}% / {fmt(s['close_mean'])}%；上涨占比 {rate(s['close_up_rate'])}。",
             f"Δ 分位 P1/P5/P50/P95/P99：{', '.join(fmt(s['delta_quantiles'][f'p{x}']) for x in (1,5,50,95,99))} pp。",
             f"Δ/L 分位：{', '.join(fmt(s['delta_over_limit_quantiles'][f'p{x}']) for x in (1,5,50,95,99))}。左/右尾 Δ≤−8/≥+8：{s['left_tail_delta_le_minus_8']}/{s['right_tail_delta_ge_8']}；Δ/L≤−0.5/≥+0.5：{s['left_tail_scaled_le_minus_05']}/{s['right_tail_scaled_ge_05']}；收盘涨/跌停 {s['close_limit_up_count']}/{s['close_limit_down_count']}（一字 {s['one_word_limit_up_count']}/{s['one_word_limit_down_count']}）。",
             f"昨板 n={data['bins']['yest_pool']['n']}，收盘均值 {fmt(data['bins']['yest_pool']['delta_over_limit_mean'])} Δ/L，红盘率 {rate(data['bins']['yest_pool']['close_green_rate'])}，断板 {s['yest_pool_break_count']}。",
             f"同日型历史：{history.get('same_regime_days', 0)} 日；历史市场中位均值 {fmt(history.get('same_regime_market_median'))}%；历史大面率均值 {rate(history.get('same_regime_big_loss_rate'))}。", "",
             "## 大肉 / 大面", "", "封停标的竞价不可买入。匹配额单位亿元，未匹配金额单位万元。", ""]
    for title, key in (("大肉前30", "big_meat"), ("大面前30", "big_loss")):
        lines += [f"### {title}", "", "代码 | 名称 | 主板块 | L% | 竞价% | 0932% | 0945% | 收盘% | Δpp | Δ/L | 高/低% | 竞价额亿/名次 | 未匹配买/卖万 | limit_state | 昨板连板 | rel | FINAL状态", "---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---"]
        for r in data[key]:
            lines.append(f"{r['symbol']} | {r.get('name') or '—'} | {r.get('main_plate') or '—'} | {plain(r.get('limit_pct'))} | {fmt(r.get('auction_pct'))} | {fmt(r.get('p0932_pct'))} | {fmt(r.get('p0945_pct'))} | {fmt(r.get('close_pct'))} | {fmt(r.get('delta_pp'))} | {fmt(r.get('delta_over_limit'))} | {fmt(r.get('high_pct'))}/{fmt(r.get('low_pct'))} | {plain((r.get('auction_amount_yuan') or 0)/1e8)}/{r.get('auction_amount_rank','—')} | {plain((r.get('rest_bid_yuan') or 0)/1e4)}/{plain((r.get('rest_ask_yuan') or 0)/1e4)} | {r.get('auction_limit_state','—')} | {r.get('lb_days',0)} | {fmt(r.get('rel_to_strongest_plate'))} | {r.get('auction_price_status')}")
        lines.append("")
    lines += ["## 极值路径", ""]
    for label, rows in data["paths"].items():
        items = []
        for r in rows:
            item = f"{r['symbol']} {r.get('name') or ''}".strip()
            if label == "竞价封跌": item += f"(0932 {fmt(r.get('p0932_pct'))}%, 0945 {fmt(r.get('p0945_pct'))}%, 收盘 {fmt(r.get('close_pct'))}%, 最低 {fmt(r.get('low_pct'))}%)"
            items.append(item)
        lines.append(f"- {label}: {len(rows)} — " + (", ".join(items) if items else "当日无"))
    lines += ["", "## 分档", ""]
    for group_name, groups in data["bins"].items():
        if not isinstance(groups, dict) or "n" in groups: continue
        lines += [f"### {group_name}", "", "档 | n | Δ/L均值 | 中位 | 大肉率 | 大面率 | 红盘率", "---|---:|---:|---:|---:|---:|---:"]
        for label, p in groups.items(): lines.append(f"{label} | {p['n']} | {fmt(p['delta_over_limit_mean'])} | {fmt(p['delta_over_limit_median'])} | {rate(p['big_meat_rate'])} | {rate(p['big_loss_rate'])} | {rate(p['close_green_rate'])}")
        lines.append("")
    lines += [f"抢跑代理(rel≥+2)大面率 {rate(data['diagnostics']['rel_ge_2_big_loss_rate'])} vs 基线 {rate(data['diagnostics']['baseline_big_loss_rate'])}；深度低开(竞价≤−3)大肉率 {rate(data['diagnostics']['auction_le_minus_3_big_meat_rate'])} vs 基线 {rate(data['diagnostics']['baseline_big_meat_rate'])}。4 日线索，未证明稳健。", "",
             "## 板块聚合", "", ("成员数≥8 的非泛化板块：" if data["plates"].get("availability") == "READY" else "缺少 --relation-dir，无法计算成员级板块聚合；不是零个板块。"),
             (f"竞价最强10板块中收盘弱于竞价 {data['plates']['strongest_close_below_auction_count']} 个。" if data["plates"].get("availability") == "READY" else ""), "",
             "板块 | 成员 | Δ/L均值 | 竞价均值 | 收盘均值 | 大肉/大面", "---|---:|---:|---:|---:|---:"]
    for title, key in (("Δ/L最高", "best_delta_over_limit"), ("Δ/L最低", "worst_delta_over_limit"), ("竞价最强", "strongest_auction")):
        for r in data["plates"][key]: lines.append(f"{title} {r.get('name') or r.get('code')} | {r['members']} | {fmt(r['delta_over_limit_mean'])} | {fmt(r['auction_pct_mean'])} | {fmt(r['close_pct_mean'])} | {r['big_meat_count']}/{r['big_loss_count']}")
    daily_history = data.get("daily_history")
    if daily_history:
        lines += ["", "## 日型内分档（历史日线）", "", "日型是首要条件变量：同一信号在不同日型下方向相反。分组按全部日线历史计算；日型天数少于 3 天标为样本不足。", ""]
        for key, table in daily_history.get("regime_tables", {}).items():
            lines += [f"### {REGIME_LABELS.get(key, key)}：{table['sample_days']} 日 / {table['sample_stocks']} 只（{table['status']}）", "",
                      "开盘分组 | n | Δ/L均值 | 大肉率 | 大面率", "---|---:|---:|---:|---:"]
            for label, title in (("open_le_minus_3", "开盘≤−3%"), ("open_ge_plus_3", "开盘≥+3%"), ("market_baseline", "全市场基线")):
                p = table["open_buckets"][label]
                lines.append(f"{title} | {p['n']} | {fmt(p['delta_over_limit_mean'])} | {rate(p['big_meat_rate'])} | {rate(p['big_loss_rate'])}")
            lines += ["", "前20日收盘区间分位 | n | Δ/L均值 | 大肉率 | 大面率 | 当日收盘中位", "---|---:|---:|---:|---:|---:"]
            for q, p in table["prior_close_position_quintiles"].items():
                lines.append(f"{q} | {p['n']} | {fmt(p['delta_over_limit_mean'])} | {rate(p['big_meat_rate'])} | {rate(p['big_loss_rate'])} | {fmt(p.get('close_median'))}")
            for label, p in (("昨板", table["yest_limit_pool"]), ("≥3板", table["prior_three_plus_limit"])):
                lines.append(f"{label}：n={p['n']}，开盘溢价均值 {fmt(p['open_premium_mean'])}%，收盘均值 {fmt(p['close_mean'])}%，大面率 {rate(p['big_loss_rate'])}，断板率 {rate(p['break_rate'])}（{p['status']}）。")
            lines.append("")
        lines += ["## 极致恐慌→修复序列（日线）", "", "T/T+1/T+2/T+3 的市场中位、上涨占比、涨跌停、左右尾和 T 日跌停股后续表现：", ""]
        for seq in daily_history.get("panic_sequences", []):
            lines += [f"### 恐慌日 {seq['panic_date']}", "", "时点 | 日期 | 市场中位 | 上涨占比 | 涨停/跌停 | 左/右尾 | T跌停股开盘均值 | 收盘均值 | 红盘率 | 再跌停 | 当日触跌数/收盘转正率", "---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:"]
            for session in seq["sessions"]:
                p = session.get("summary", {})
                offset = session["offset"]
                suffix = "（修复观察日）" if data.get("trade_date") == session.get("trade_date") and offset in (1, 2) else ""
                lines.append(f"T+{offset}{suffix} | {session.get('trade_date') or '—'} | {fmt(p.get('market_median'))} | {rate(p.get('up_rate'))} | {p.get('limit_up_count','—')}/{p.get('limit_down_count','—')} | {p.get('left_tail_count','—')}/{p.get('right_tail_count','—')} | {fmt(p.get('panic_down_open_mean'))} | {fmt(p.get('panic_down_close_mean'))} | {rate(p.get('panic_down_close_green_rate'))} | {p.get('panic_down_limit_again_count','—')} | {p.get('hit_down_count','—')}/{rate(p.get('hit_down_close_green_rate'))}")
            lines.append("")
    lines += ["", 
             "## 多日对比", "", f"历史样本 {history['sample_days']} 日；今日左尾数历史分位 {plain((history['today_left_tail_historical_percentile'] or 0)*100,1)}%。", "",
             "日期 | 市场中位 | 上涨占比 | Δ中位 | ΔP5 | ΔP95 | 左/右尾 | 大肉/大面 | 昨板大面率 | 抢跑大面/基线 | 深低开大肉/基线 | 最强10板块Δ/L均值 | 收盘低于竞价", "---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:"]
    for r in history["rows"]:
        lines.append(f"{r['trade_date']} | {fmt(r['market_median'])} | {rate(r['up_rate'])} | {fmt(r['delta_median'])} | {fmt(r['delta_p5'])} | {fmt(r['delta_p95'])} | {r['left_tail_count']}/{r['right_tail_count']} | {r['big_meat_count']}/{r['big_loss_count']} | {rate(r['yest_pool_big_loss_rate'])} | {rate(r['rel_big_loss_rate'])}/{rate(r['baseline_big_loss_rate'])} | {rate(r['deep_low_big_meat_rate'])}/{rate(r['baseline_big_meat_rate'])} | {fmt(r['strongest_plates_mean_delta_l'])} | {r['strongest_plates_close_below_auction']}")
    lines += ["", "## 口径说明", "", "日型规则：恐慌日左尾Δ≤−8pp家数≥300；修复/强势日右尾Δ≥+8pp家数≥200且市场中位>0；弱势日上涨占比≤25%或市场中位≤−1.2%（且非恐慌）；其余常态日。日型是首要条件变量：同一信号在不同日型下方向相反。Δ=收盘涨跌幅−0925涨跌幅；Δ/L 以涨跌停幅度标准化。竞价涨跌幅取 0925；FINAL 表示最后 tick 不早于09:25:00，INDICATIVE 为更早指示价。竞价为虚拟撮合，无成交量额；未匹配买卖为二档。涨停需有封单，竞价 limit_state 表示封停。没有逐笔委托，抢跑/抢筹仅为聚合推断。此报告为事后复盘，不是预测；分档阈值仅用于展示。收盘数据只用于本独立复盘。", ""]
    return "\n".join(lines)


def render_daily_history_markdown(data: Mapping[str, Any]) -> str:
    signed = lambda x: "—" if x is None else f"{x:+.2f}"
    rate = lambda x: "—" if x is None else f"{x*100:.1f}%"
    lines = ["# 日线极值历史复盘", "", "模式：仅日线。竞价分档、抢跑代理和竞价最强板块不可得，保持缺失。",
             "日型是首要条件变量：同一信号在不同日型下方向相反。规则：恐慌日左尾Δ≤−8pp家数≥300；修复/强势日右尾Δ≥+8pp家数≥200且市场中位>0；弱势日上涨占比≤25%或市场中位≤−1.2%（且非恐慌）；其余常态日。", "",
             "日期 | 日型 | n | 市场中位 | 上涨占比 | Δ中位 | ΔP5/P95 | 左/右尾 | 涨停/跌停 | 大肉/大面 | 昨板收盘均值/大面率 | 地天/近地天/天地", "---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:"]
    for day in data["days"]:
        s = day["summary"]
        paths = day.get("paths", {})
        left = s['left_tail_count'] if s['left_tail_count'] is not None else '—'
        right = s['right_tail_count'] if s['right_tail_count'] is not None else '—'
        lines.append(f"{day['trade_date']} | {REGIME_LABELS.get(day['regime'], day['regime'])} | {s['n']} | {signed(s['market_median'])} | {rate(s['up_rate'])} | {signed(s['delta_median'])} | {signed(s['delta_p5'])}/{signed(s['delta_p95'])} | {left}/{right} | {s['limit_up_count']}/{s['limit_down_count']} | {s['big_meat_count']}/{s['big_loss_count']} | {signed(s['yest_pool_close_mean'])}/{rate(s['yest_pool_big_loss_rate'])} | {paths.get('地天',0)}/{paths.get('近地天',0)}/{paths.get('天地',0)}")
    for seq in data.get("panic_sequences", []):
        for s in seq["sessions"]:
            if s.get("offset") in (1, 2):
                summary = s.get("summary", {})
                lines.append(f"修复观察 {s.get('trade_date')} (T+{s['offset']})：当日触及跌停价 {summary.get('hit_down_count', 0)} 只，收盘转正率 {rate(summary.get('hit_down_close_green_rate'))}。")
    return "\n".join(lines) + "\n"


__all__ = ["CONTRACT", "limit_percent", "normalize_extreme", "derive_extremes_review", "compare_history", "assemble_from_files", "render_extremes_markdown"]
