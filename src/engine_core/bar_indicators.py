"""Windowed auction/opening bars and rule-based turning points.

All thresholds are review prompts and remain explicitly uncalibrated.
"""
from __future__ import annotations

import html
import statistics
import time
from collections import defaultdict
from bisect import bisect_right
from functools import lru_cache
from typing import Any, Iterable, Mapping

from .auction_tracking import Tick, _at_limit, _seconds, _stamp, derive_state

WINDOWS_MINUTES = (1, 2, 5, 15)
PEAK_MIN_AMOUNT_YUAN = 1_000_000  # 待校准
PEAK_MIN_CHANGE_PP = 0.5  # 待校准
ACCELERATION_MULTIPLE = 2.0  # 待校准
FLIP_MIN_CHANGE_PP = 0.5  # 待校准
PLATE_SHARE_FLIP_BP = 50  # 待校准
LATE_RUSH_SHARE_PCT = 40.0  # 待校准
LATE_RUSH_EXCESS_PCT = 10.0  # 板块相对大盘的尾段集中度阈值，待校准
EARLY_FAVORITE_BEFORE = "09:22:00"  # 待校准
FADE_AMOUNT_MULTIPLE = 0.5  # 待校准


@lru_cache(maxsize=4096)
def _cached_seconds(ts: str) -> int:
    return _seconds(ts)


def _price_pct(price: int, close: int) -> float | None:
    return (price / close - 1) * 100 if price > 0 and close > 0 else None


def detect_turning_points(rows: list[Mapping[str, Any]], *, flip_min: float = FLIP_MIN_CHANGE_PP,
                         peak_min: float | None = None) -> list[dict[str, Any]]:
    """Detect material local amount extrema, sustained acceleration, and pct flips."""
    if peak_min is None:
        peak_min = PEAK_MIN_AMOUNT_YUAN
    values = [float(r["window_amount_yuan"]) for r in rows if r.get("window_amount_yuan") is not None]
    median_value = statistics.median(values) if values else 0.0
    volatility = statistics.median(abs(value - median_value) for value in values) if values else 0.0
    relative_floor = 2 * volatility
    pct_values = [float(r["window_pct_change_pp"]) for r in rows if r.get("window_pct_change_pp") is not None]
    pct_median = statistics.median(pct_values) if pct_values else 0.0
    pct_volatility = statistics.median(abs(value - pct_median) for value in pct_values) if pct_values else 0.0
    points: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        amount = row.get("window_amount_yuan")
        if i >= 2 and i + 1 < len(rows) and amount is not None:
            before = rows[i - 1].get("window_amount_yuan")
            after = rows[i + 1].get("window_amount_yuan")
            if before is not None and after is not None:
                amplitude = amount - max(before, after) if amount > before and amount >= after else (
                    min(before, after) - amount if amount < before and amount <= after else 0)
                peak = amount > before and amount >= after and amplitude >= max(peak_min, relative_floor)
                valley = amount < before and amount <= after and amplitude >= max(peak_min, relative_floor)
                if peak or valley:
                    points.append({"kind": "AMOUNT_PEAK" if peak else "AMOUNT_VALLEY",
                        "timestamp": row["timestamp"], "before": before, "value": amount, "after": after,
                        "window_amount_yuan": amount,
                        "amplitude": amplitude, "volatility_yuan": volatility,
                        "salience": amplitude / max(peak_min, relative_floor, 1e-12),
                        "window_minutes": row.get("window_minutes")})
        if i >= 2:
            prev2, prev1 = rows[i - 2].get("window_amount_yuan"), rows[i - 1].get("window_amount_yuan")
            cur = amount
            if prev2 is not None and prev1 is not None and cur is not None and prev2 > 0 and prev1 > 0:
                first = prev1 / prev2
                second = cur / prev1
                acceleration_amplitude = cur - prev2
                if first >= ACCELERATION_MULTIPLE and second >= ACCELERATION_MULTIPLE and acceleration_amplitude >= max(peak_min, relative_floor):
                    points.append({"kind": "ACCELERATION_START", "timestamp": rows[i - 1]["timestamp"],
                        "before": prev2, "value": prev1, "after": cur,
                        "window_amount_yuan": cur,
                        "amplitude": acceleration_amplitude, "volatility_yuan": volatility,
                        "salience": acceleration_amplitude / max(peak_min, relative_floor, 1e-12),
                        "window_minutes": row.get("window_minutes")})
                    # Only the first bar in a sustained run is reported.
                    if i >= 3:
                        p3 = rows[i - 3].get("window_amount_yuan")
                        if p3 and prev2 / p3 >= ACCELERATION_MULTIPLE and prev1 / prev2 >= ACCELERATION_MULTIPLE:
                            points.pop()
        if i:
            before_pct, current_pct = rows[i - 1].get("window_pct_change_pp"), row.get("window_pct_change_pp")
            if before_pct is not None and current_pct is not None:
                flipped = ((before_pct < 0 <= current_pct) or (before_pct > 0 >= current_pct))
                if flipped and abs(current_pct - before_pct) >= max(flip_min, 2 * pct_volatility):
                    points.append({"kind": "DIRECTION_FLIP", "timestamp": row["timestamp"],
                        "before": before_pct, "value": current_pct, "after": current_pct,
                        "window_amount_yuan": amount,
                        "amplitude": abs(current_pct - before_pct), "volatility_yuan": volatility,
                        "salience": abs(current_pct - before_pct) / max(flip_min, 2 * pct_volatility, 1e-12),
                        "window_minutes": row.get("window_minutes")})
    points.sort(key=lambda p: (-float(p.get("salience", 0)), p["timestamp"]))
    return sorted(points[:5], key=lambda p: p["timestamp"])


def summarize_curve(rows: list[Mapping[str, Any]], *, market_rows: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    auction = [r for r in rows if r.get("phase") == "AUCTION" and r.get("cumulative_amount_yuan") is not None]
    terminal = auction[-1].get("cumulative_amount_yuan") if auction else None
    target = terminal * 0.5 if terminal is not None else None
    t50 = next((r["timestamp"] for r in auction if target is not None and r["cumulative_amount_yuan"] >= target), None)
    last = None
    if auction and terminal is not None:
        terminal_sec = _seconds(str(auction[-1]["timestamp"])[-8:])
        prior = next((r for r in reversed(auction[:-1]) if _seconds(str(r["timestamp"])[-8:]) <= terminal_sec - 60), None)
        if prior and prior.get("cumulative_amount_yuan") is not None:
            last = terminal - prior["cumulative_amount_yuan"]
    last_share = 100 * last / terminal if terminal and last is not None else None
    market_summary = summarize_curve(market_rows) if market_rows is not None and market_rows is not rows else None
    market_late = (last_share if market_rows is rows else market_summary.get("last_60s_share_pct")
                   if market_summary else None)
    excess = round(last_share - market_late, 2) if last_share is not None and market_late is not None else None
    market_t50 = t50 if market_rows is rows else market_summary.get("t50_timestamp") if market_summary else None
    t50_offset = (_seconds(t50[-8:]) - _seconds(market_t50[-8:])) if t50 and market_t50 else None
    labels = []
    if (excess is not None and excess >= LATE_RUSH_EXCESS_PCT) or (
            market_rows is None and last_share is not None and last_share >= LATE_RUSH_SHARE_PCT):
        labels.append("LATE_RUSH")
    if t50 and t50[-8:] < EARLY_FAVORITE_BEFORE:
        labels.append("EARLY_FAVORITE")
    flips = sum(p["kind"] == "DIRECTION_FLIP" for p in detect_turning_points(auction))
    if flips >= 2:
        labels.append("REVERSAL")
    return {"t50_timestamp": t50, "t50_vs_market_seconds": t50_offset,
            "last_60s_share_pct": round(last_share, 2) if last_share is not None else None,
            "last_60s_excess_vs_market_pp": excess,
            "amount_peak_timestamp": next((p["timestamp"] for p in detect_turning_points(auction) if p["kind"] == "AMOUNT_PEAK"), None),
            "direction_flip_count": flips, "labels": labels, "terminal_amount_yuan": terminal}


def build_bar_report(*, trade_date: str, auction_ticks: Iterable[Tick], open_ticks: Iterable[Tick] = (),
                     relations: Mapping[str, set[str]] | None = None, plate_names: Mapping[str, str] | None = None,
                     generic_names: set[str] | None = None, pool_symbols: set[str] | None = None,
                     prev_close: Mapping[str, Any] | None = None,
                     baseline: Mapping[str, Any] | None = None, full_json: bool = False) -> dict[str, Any]:
    """Build compact market/plate/focus-stock rolling bars and report fragments."""
    relations, plate_names, generic_names = relations or {}, plate_names or {}, generic_names or set()
    auction = sorted((t for t in auction_ticks if t.trade_date == trade_date), key=lambda t: (_cached_seconds(t.ts), t.symbol))
    opened = sorted((t for t in open_ticks if t.trade_date == trade_date), key=lambda t: (_cached_seconds(t.ts), t.symbol))
    state: dict[str, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []
    ai = 0
    for sec in range(_seconds("09:15:00"), _seconds("09:25:00") + 1, 30):
        while ai < len(auction) and _cached_seconds(auction[ai].ts) <= sec:
            state[auction[ai].symbol] = derive_state(auction[ai]); ai += 1
        samples.append({"sec": sec, "phase": "AUCTION", "states": dict(state)})
    auction_anchor = dict(state)
    oi = 0
    for sec in range(_seconds("09:30:00"), _seconds("09:45:00") + 1, 60):
        while oi < len(opened) and _cached_seconds(opened[oi].ts) <= sec:
            tick = opened[oi]
            row = derive_state(tick)
            row["match_amount_yuan"] = max(0, int(tick.amt_yuan))
            state[tick.symbol] = row
            oi += 1
        samples.append({"sec": sec, "phase": "OPEN", "states": dict(state)})
    terminal = samples[20]["states"] if len(samples) > 20 else (samples[-1]["states"] if samples else {})
    stock_amounts = [(s, int(v.get("match_amount_yuan") or 0)) for s, v in terminal.items()]
    pool_symbols = set(pool_symbols or ())
    ranked_symbols = [s for s, _ in sorted(stock_amounts, key=lambda x: (-x[1], x[0]))]
    top50_symbols = set(ranked_symbols[:50])
    focus = pool_symbols | set(ranked_symbols[:50])
    prev_close = prev_close or {}
    for sample_index, snap in enumerate(samples):
        for symbol, row in snap["states"].items():
            close = row.get("prev_close_milli") or prev_close.get(symbol)
            if isinstance(close, Mapping): close = close.get("prev_close_milli", close.get("pc_milli"))
            row["pct_change"] = _price_pct(int(row.get("price_milli") or 0), int(close or 0))
    aggregate_started = time.perf_counter()
    matrix_codes = [code for code in relations if plate_names.get(code, code) not in generic_names]
    matrix_stocks = sorted({symbol for code in matrix_codes for symbol in relations[code]})
    matrix_result = None
    if matrix_codes and matrix_stocks:
        import numpy as np
        from .plate_matrix import PlateMatrix

        stock_index = {symbol: index for index, symbol in enumerate(matrix_stocks)}
        plate_matrix = PlateMatrix(
            ((code, symbol) for code in matrix_codes for symbol in relations[code]),
            matrix_codes, matrix_stocks)
        matrix_result = {
            key: np.empty((len(matrix_codes), 6, len(samples)), dtype=np.int64 if key == "count" else np.float64)
            for key in ("sum", "mean", "count")}
        for sample_index, snap in enumerate(samples):
            sample_values = np.full((len(matrix_stocks), 6), np.nan, dtype=np.float64)
            for symbol, value in snap["states"].items():
                index = stock_index.get(symbol)
                pct = value.get("pct_change")
                if index is None or pct is None:
                    continue
                sample_values[index, :] = (
                    pct, int(value.get("match_amount_yuan") or 0),
                    int(_at_limit(value, "up")), int(_at_limit(value, "down")),
                    int(value.get("status") == "FINAL"), int(value.get("status") == "INDICATIVE"))
            sample_result = plate_matrix.aggregate(sample_values, weight_index=1)
            for key in matrix_result:
                matrix_result[key][:, :, sample_index] = sample_result[key][:, :, 0]
    # Build point-in-time aggregates once per sample. Missing inputs stay null.
    aggregates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample_index, snap in enumerate(samples):
        states = snap["states"]
        stock_amounts_now = [v.get("match_amount_yuan") for v in states.values() if v.get("match_amount_yuan") is not None]
        pcts = [v["pct_change"] for v in states.values() if v.get("pct_change") is not None]
        aggregates["market"].append({"sec": snap["sec"], "phase": snap["phase"],
            "cum_amount": sum(stock_amounts_now) if stock_amounts_now else None,
            "pct": statistics.median(pcts) if pcts else None,
            "up": sum(_at_limit(v, "up") for v in states.values()),
            "down": sum(_at_limit(v, "down") for v in states.values()),
            "final_count": sum(v.get("status") == "FINAL" for v in states.values()),
            "indicative_count": sum(v.get("status") == "INDICATIVE" for v in states.values())})
        if matrix_result is not None:
            for plate_index, code in enumerate(matrix_codes):
                if matrix_result["count"][plate_index, 0, sample_index] < 5:
                    continue
                mean_pct, total, up, down, final, indicative = (
                    matrix_result["sum"][plate_index, feature, sample_index]
                    if feature != 0 else matrix_result["mean"][plate_index, feature, sample_index]
                    for feature in range(6))
                aggregates[f"plate:{code}"].append({"sec": snap["sec"], "phase": snap["phase"],
                    "cum_amount": int(total), "pct": float(mean_pct), "up": int(up), "down": int(down),
                    "final_count": int(final), "indicative_count": int(indicative)})
        for symbol in focus:
            v = states.get(symbol)
            if not v: continue
            aggregates[f"stock:{symbol}"].append({"sec": snap["sec"], "phase": snap["phase"],
                "cum_amount": v.get("match_amount_yuan"), "pct": v.get("pct_change"),
                "price": v.get("price_milli"), "auction_price": auction_anchor.get(symbol, {}).get("price_milli"),
                "up": int(_at_limit(v, "up")), "down": int(_at_limit(v, "down"))})
    aggregate_seconds = time.perf_counter() - aggregate_started
    turning_started = time.perf_counter()
    bars: list[dict[str, Any]] = []
    curves: dict[str, list[dict[str, Any]]] = {}
    market_points = {(x["phase"], x["sec"]): x for x in aggregates["market"]}
    for key, curve in aggregates.items():
        curves[key] = []
        auction_terminal_for_key = next((x["cum_amount"] for x in reversed(curve) if x["phase"] == "AUCTION"), None)
        windows = (0.5, *WINDOWS_MINUTES) if key == "market" else WINDOWS_MINUTES
        phase_points: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in curve:
            phase_points[item["phase"]].append(item)
        phase_seconds = {phase: [item["sec"] for item in items] for phase, items in phase_points.items()}
        for window in windows:
            prior_amount: dict[str, int | None] = {}
            for cur in curve:
                if cur["phase"] == "AUCTION" and window == 15: continue
                interval = window * 60
                if cur["phase"] == "OPEN" and (window > 15 or window == 0.5): continue
                same_phase = phase_points[cur["phase"]]
                target_index = bisect_right(phase_seconds[cur["phase"]], cur["sec"] - interval) - 1
                base = same_phase[target_index] if target_index >= 0 else None
                if cur["sec"] % interval: continue
                amount = cur["cum_amount"] - base["cum_amount"] if base and cur["cum_amount"] is not None and base["cum_amount"] is not None else None
                pct = cur["pct"] - base["pct"] if base and cur["pct"] is not None and base["pct"] is not None else None
                market_now = market_points.get((cur["phase"], cur["sec"]))
                market_base = market_points.get((cur["phase"], base["sec"])) if base else None
                market_amount = market_now["cum_amount"] - market_base["cum_amount"] if market_now and market_base and market_now["cum_amount"] is not None and market_base["cum_amount"] is not None else None
                row = {"object_type": key.split(":")[0], "object": key.split(":", 1)[1] if ":" in key else "ALL",
                    "timestamp": _stamp(trade_date, cur["sec"]), "phase": cur["phase"], "window_minutes": window,
                    "window_amount_yuan": amount, "window_pct_change_pp": pct,
                    "market_share_bp": round(amount * 10000 / market_amount, 2) if amount is not None and market_amount else None,
                    "amount_multiple": None, "amount_multiple_basis": None,
                    "end_pct": cur["pct"], "end_price_milli": cur.get("price"),
                    "limit_up_count": cur["up"], "limit_down_count": cur["down"],
                    "final_count": cur.get("final_count"), "indicative_count": cur.get("indicative_count"),
                    "cumulative_amount_yuan": cur["cum_amount"]}
                prev_amount = prior_amount.get(cur["phase"])
                if amount is not None and prev_amount:
                    row["amount_multiple"] = round(amount / prev_amount, 3)
                    row["amount_multiple_basis"] = "PREVIOUS_WINDOW"
                elif amount is not None and cur["phase"] == "OPEN":
                    if auction_terminal_for_key:
                        row["amount_multiple"] = round(amount / auction_terminal_for_key, 3)
                        row["amount_multiple_basis"] = "AUCTION_TERMINAL_FALLBACK"
                    else:
                        row["amount_multiple_basis"] = "MISSING_NO_PREVIOUS_WINDOW_OR_AUCTION_TERMINAL"
                if pct is not None:
                    row["window_pct_change_pp"] = round(pct, 6)
                prior_amount[cur["phase"]] = amount
                bars.append(row); curves[key].append(row)
    plate_rank = sorted((code for code in relations if f"plate:{code}" in aggregates),
        key=lambda code: (-int(next((x["cum_amount"] or 0 for x in reversed(aggregates[f"plate:{code}"]) if x["phase"] == "AUCTION"), 0)), code))
    top30_plates = set(plate_rank[:30])
    turning_keys = {"market", *(f"plate:{code}" for code in top30_plates),
                    *(f"stock:{symbol}" for symbol in focus)}
    turns = []
    for key, rows in curves.items():
        if not full_json and key not in turning_keys:
            continue
        for window in WINDOWS_MINUTES:
            for phase in ("AUCTION", "OPEN"):
                selected = [r for r in rows if r["window_minutes"] == window and r["phase"] == phase]
                for point in detect_turning_points(selected):
                    point.update({"object_type": key.split(":")[0], "object": key.split(":", 1)[1] if ":" in key else "ALL", "phase": phase})
                    if not (phase == "AUCTION" and point["kind"] == "DIRECTION_FLIP" and point["timestamp"][-8:] < "09:20:00"):
                        turns.append(point)
                if key.startswith("plate:"):
                    share_rows = [{**r, "window_amount_yuan": r["market_share_bp"],
                                   "window_pct_change_pp": r["market_share_bp"]}
                                  for r in selected if r.get("market_share_bp") is not None]
                    for point in detect_turning_points(share_rows, flip_min=PLATE_SHARE_FLIP_BP,
                                                       peak_min=PLATE_SHARE_FLIP_BP):
                        point["kind"] = "SHARE_" + point["kind"]
                        point["series"] = "market_share_bp"
                        point.update({"object_type": "plate", "object": key.split(":", 1)[1], "phase": phase})
                        if not (phase == "AUCTION" and point["timestamp"][-8:] < "09:20:00"):
                            turns.append(point)
    summaries = []
    market_curve_rows = [{"timestamp": _stamp(trade_date, x["sec"]), "phase": x["phase"],
        "cumulative_amount_yuan": x["cum_amount"], "window_amount_yuan": None,
        "window_pct_change_pp": x["pct"]} for x in aggregates["market"]]
    for key, curve in aggregates.items():
        if key.startswith("stock:") and key.split(":", 1)[1] not in focus: continue
        curve_rows = [{"timestamp": _stamp(trade_date, x["sec"]), "phase": x["phase"],
            "cumulative_amount_yuan": x["cum_amount"], "window_amount_yuan": None,
            "window_pct_change_pp": x["pct"]} for x in curve]
        summary = summarize_curve(curve_rows, market_rows=market_curve_rows)
        one_minute = [b for b in curves[key] if b["window_minutes"] == 1 and b["phase"] == "AUCTION"]
        peaks = [p for p in detect_turning_points(one_minute) if p["kind"] == "AMOUNT_PEAK"]
        summary["amount_peak_timestamp"] = peaks[0]["timestamp"] if peaks else None
        open5 = next((b for b in curves[key] if b["window_minutes"] == 5 and b["timestamp"].endswith("09:35:00")), None)
        auction_terminal = next((x["cum_amount"] for x in reversed(curve) if x["phase"] == "AUCTION"), None)
        if open5 and auction_terminal and open5.get("window_amount_yuan") is not None and open5["window_amount_yuan"] / auction_terminal < FADE_AMOUNT_MULTIPLE and (open5.get("window_pct_change_pp") or 0) < 0:
            summary["labels"].append("FADE")
        opening_metrics = {}
        for window, mark in ((1, "09:31:00"), (5, "09:35:00"), (15, "09:45:00")):
            item = next((b for b in curves[key] if b["phase"] == "OPEN" and b["window_minutes"] == window and b["timestamp"].endswith(mark)), None)
            if item:
                opening_metrics[str(window)] = {"amount_yuan": item["window_amount_yuan"],
                    "amount_multiple": item["amount_multiple"],
                    "amount_multiple_basis": item.get("amount_multiple_basis"),
                    "pct_change_pp": item["window_pct_change_pp"]}
        summary["opening_windows"] = opening_metrics
        if key.startswith("plate:"):
            def share_at(mark: str) -> float | None:
                point = next((x for x in curve if x["phase"] == "AUCTION" and x["sec"] == _seconds(mark)), None)
                market_point = next((x for x in aggregates["market"] if x["phase"] == "AUCTION" and x["sec"] == _seconds(mark)), None)
                return round(point["cum_amount"] * 10000 / market_point["cum_amount"], 2) if point and market_point and market_point["cum_amount"] else None
            share_920, share_925 = share_at("09:20:00"), share_at("09:25:00")
            summary["share_0920_bp"] = share_920
            summary["share_0925_bp"] = share_925
            summary["share_change_0920_0925_bp"] = round(share_925 - share_920, 2) if share_920 is not None and share_925 is not None else None
        if key.startswith("stock:"):
            symbol = key.split(":", 1)[1]
            auction_price = auction_anchor.get(symbol, {}).get("price_milli")
            trace = [x for x in curve if x["phase"] == "OPEN" and x.get("price") and auction_price]
            summary["auction_price_milli"] = auction_price
            prior_price = auction_price
            first_below = first_above = None
            for item in trace:
                price = item["price"]
                if first_below is None and prior_price >= auction_price and price < auction_price:
                    first_below = _stamp(trade_date, item["sec"])
                if first_above is None and prior_price < auction_price and price >= auction_price:
                    first_above = _stamp(trade_date, item["sec"])
                prior_price = price
            summary["first_below_auction_timestamp"] = first_below
            summary["first_above_auction_timestamp"] = first_above
            summary["in_yesterday_limit_pool"] = symbol in pool_symbols
            summary["top50_auction_amount"] = symbol in top50_symbols
            summary["sealed_0925"] = bool(auction_anchor.get(symbol) and
                (_at_limit(auction_anchor[symbol], "up") or _at_limit(auction_anchor[symbol], "down")))
        object_type = key.split(":")[0]
        object_code = key.split(":", 1)[1] if ":" in key else "ALL"
        object_name = _plate_display_name(object_code, plate_names) if object_type == "plate" else object_code
        summaries.append({"object_type": object_type, "object": object_code,
                          "object_name": object_name, **summary})
    by_object: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for point in turns:
        by_object[(point["object_type"], point["object"])].append(point)
    turns = []
    for points in by_object.values():
        points.sort(key=lambda p: (-float(p.get("salience", 0)), p["timestamp"]))
        turns.extend(sorted(points[:5], key=lambda p: p["timestamp"]))
    baseline = baseline or {}
    prior_days = sorted(str(day) for day in baseline if str(day) < trade_date)
    baseline_amount = int(baseline.get(prior_days[-1]) or 0) if prior_days else None
    turning_seconds = time.perf_counter() - turning_started
    render_started = time.perf_counter()
    html_report = _render_html(trade_date, bars, summaries, turns, plate_names, baseline_amount)
    md = _render_markdown(trade_date, summaries, turns, bars)
    render_seconds = time.perf_counter() - render_started
    if not full_json:
        summaries = [s for s in summaries if s["object_type"] == "market" or
            (s["object_type"] == "plate" and s["object"] in top30_plates) or
            (s["object_type"] == "stock" and s["object"] in focus)]
        bars = [b for b in bars if b["object_type"] == "market" or
            (b["object_type"] == "plate" and b["object"] in top30_plates) or
            (b["object_type"] == "stock" and b["object"] in focus)]
    return {"trade_date": trade_date, "json_mode": "full" if full_json else "focus", "bars": bars, "turning_points": turns, "summaries": summaries,
            "html": html_report, "markdown": md, "focus_symbols": sorted(focus),
            "previous_day_terminal_amount_yuan": baseline_amount,
            "_stage_timings": {"plate_aggregation_seconds": aggregate_seconds,
                               "turning_point_seconds": turning_seconds,
                               "render_seconds": render_seconds}}


def _plate_display_name(code: str, names: Mapping[str, str]) -> str:
    name = str(names.get(code, "")).strip()
    return f"{name} · {code}" if name and name != code else f"{code} (无名)"


def _render_html(trade_date: str, bars: list[dict[str, Any]], summaries: list[dict[str, Any]],
                 turns: list[dict[str, Any]], names: Mapping[str, str], baseline_amount: int | None) -> str:
    plates = sorted((r for r in summaries if r["object_type"] == "plate"),
                    key=lambda r: (-(r.get("share_0925_bp") or 0), r["object"]))[:30]
    stocks = [r for r in summaries if r["object_type"] == "stock" and
              (r.get("in_yesterday_limit_pool") or r.get("sealed_0925") or r.get("top50_auction_amount"))]
    chosen = [r for r in summaries if r["object_type"] == "market"] + plates + stocks
    sections = []
    for item in chosen:
        obj = html.escape(item.get("object_name") or names.get(item["object"], item["object"]))
        if item["object_type"] == "market":
            selected = [b for b in bars if b["object_type"] == "market" and
                ((b["phase"] == "AUCTION" and b["window_minutes"] == 0.5) or
                 (b["phase"] == "OPEN" and b["window_minutes"] == 1)) and b["window_amount_yuan"] is not None]
        else:
            selected = [b for b in bars if b["object"] == item["object"] and b["window_minutes"] == 1 and b["window_amount_yuan"] is not None]
        vals = [float(b["window_amount_yuan"]) for b in selected]
        maxv = max(vals) if vals and max(vals) else 1
        pts = " ".join(f"{i * 4},{38 - 34 * v / maxv:.1f}" for i, v in enumerate(vals))
        pct_vals = [float(b["end_pct"]) for b in selected if b.get("end_pct") is not None]
        span = max(pct_vals) - min(pct_vals) if pct_vals else 0
        pctpts = " ".join(f"{i * 4},{38 - 34 * (v - min(pct_vals)) / (span or 1):.1f}" for i, v in enumerate(pct_vals))
        marks = []
        for turn in turns:
            if turn.get("object") != item["object"] or turn.get("kind") not in ("AMOUNT_PEAK", "AMOUNT_VALLEY", "ACCELERATION_START"):
                continue
            idx = next((i for i, bar in enumerate(selected) if bar["timestamp"] == turn.get("timestamp")), None)
            if idx is not None:
                marks.append(f"<circle cx='{idx * 4}' cy='6' r='2' fill='#e80'><title>{html.escape(turn['kind'])}</title></circle>")
        sections.append(f"<section><h3>{obj}</h3><svg viewBox='0 0 160 42' role='img'><polyline points='{pts}' fill='none' stroke='currentColor' stroke-width='1.5'/><polyline points='{pctpts}' fill='none' stroke='#d66' stroke-width='1'/> {''.join(marks)}</svg><small>金额 / 价格涨幅 · t50 {item.get('t50_timestamp') or '缺失'}</small></section>")
    baseline_note = f"前日 09:25 终值参考线: {baseline_amount:,} 元。" if baseline_amount is not None else "前日 09:25 终值缺失。"
    return ("<!doctype html><html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>窗口指标 {trade_date}</title><style>:root{{color-scheme:light dark}}body{{font:14px system-ui;margin:1rem}}main{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:1rem}}section{{border:1px solid #8885;border-radius:8px;padding:.6rem}}svg{{width:100%;height:70px}}</style>"
        f"<h1>大盘额曲线 · {trade_date}</h1><p>竞价虚拟撮合；09:25:00 起使用 FINAL px/amt_yuan。{baseline_note}橙点标注显著转折。板块 LATE_RUSH 按最后60秒占比相对大盘高 {LATE_RUSH_EXCESS_PCT}pp 判定。窗口额为累计额增量；窗口涨幅为端点变化，板块按等权。q2 spd1m/amt2m/amt5m 分别对应 1/2/5 分钟字段，历史值由 tick 重建。</p><main>{''.join(sections)}</main></html>")


def _render_markdown(trade_date: str, summaries: list[dict[str, Any]], turns: list[dict[str, Any]],
                     bars: list[dict[str, Any]]) -> str:
    market = next((r for r in summaries if r["object_type"] == "market"), None)
    plates = sorted((r for r in summaries if r["object_type"] == "plate"),
        key=lambda r: (-(r.get("share_0925_bp") or 0), r["object"]))[:30]
    stocks = sorted((r for r in summaries if r["object_type"] == "stock"),
        key=lambda r: (-(r.get("terminal_amount_yuan") or 0), r["object"]))[:30]
    selected = ([market] if market else []) + plates + stocks
    lines = [f"# 窗口指标 {trade_date}", "",
        "竞价为虚拟撮合；09:25:00 起 px/amt_yuan 为 FINAL。窗口为 1/2/5/15 分钟；竞价额曲线每 30 秒更新，开盘曲线每分钟更新。板块涨幅等权。",
        "q2 spd1m/amt2m/amt5m 对应 1/2/5 分钟字段；缺失不补零。开盘倍数分母优先前一窗口，前值为零时回退竞价终值并标注。",
        f"LATE_RUSH 表示最后60秒占比高于大盘 {LATE_RUSH_EXCESS_PCT}pp。", "",
        "## 大盘曲线与转折", ""]
    if market:
        lines.append(f"- t50 {market.get('t50_timestamp') or '缺失'}；最后60秒 {market.get('last_60s_share_pct')}%；显著转折 {market.get('amount_peak_timestamp') or '无'}")
    market_bars = [b for b in bars if b["object_type"] == "market" and
        ((b["phase"] == "AUCTION" and b["window_minutes"] == 0.5) or
         (b["phase"] == "OPEN" and b["window_minutes"] == 1))]
    checkpoints = {"09:15:00", "09:20:00", "09:22:00", "09:24:00", "09:25:00",
                   "09:30:00", "09:31:00", "09:32:00", "09:35:00", "09:40:00", "09:45:00"}
    market_bars = [b for b in market_bars if b["timestamp"][-8:] in checkpoints]
    lines.extend(["", "| 时点 | 阶段 | 累计额(元) | 窗口额(元) | 中位涨幅(%) | 涨停/跌停数 |",
                  "|---|---|---:|---:|---:|---:|"])
    for b in market_bars:
        pct = b.get("end_pct")
        lines.append(f"| {b['timestamp'][-8:]} | {b['phase']} | {b.get('cumulative_amount_yuan') if b.get('cumulative_amount_yuan') is not None else '缺失'} | {b.get('window_amount_yuan') if b.get('window_amount_yuan') is not None else '缺失'} | {round(pct, 3) if pct is not None else '缺失'} | {b.get('limit_up_count', 0)}/{b.get('limit_down_count', 0)} |")
    lines.extend(["", "## 板块 Top30", "", "| 板块 | t50偏移(秒) | 最后60秒占比 | 相对大盘(pp) | 份额变化(bp) | 开盘1m/5m/15m倍数 | 标签 |", "|---|---:|---:|---:|---:|---|---|"])
    for r in plates:
        opens = r.get("opening_windows", {})
        multiple = "/".join(str(opens.get(str(w), {}).get("amount_multiple", "缺失")) for w in (1, 5, 15))
        lines.append(f"| {r['object_name']} | {r.get('t50_vs_market_seconds', '缺失')} | {r.get('last_60s_share_pct', '缺失')} | {r.get('last_60s_excess_vs_market_pp', '缺失')} | {r.get('share_change_0920_0925_bp', '缺失')} | {multiple} | {', '.join(r['labels']) or '—'} |")
    lines.extend(["", "## 个股 Top30", "", "| 代码 | 竞价额 | 开盘1m/5m/15m倍数 | 1m分母来源 | 首次跌破/站回竞价价 | 昨日涨停池 |", "|---|---:|---|---|---|---|"])
    for r in stocks:
        opens = r.get("opening_windows", {})
        multiple = "/".join(str(opens.get(str(w), {}).get("amount_multiple", "缺失")) for w in (1, 5, 15))
        basis = opens.get("1", {}).get("amount_multiple_basis") or "缺失"
        crossing = f"{r.get('first_below_auction_timestamp') or '—'} / {r.get('first_above_auction_timestamp') or '—'}"
        lines.append(f"| {r['object']} | {r.get('terminal_amount_yuan', '缺失')} | {multiple} | {basis} | {crossing} | {'是' if r.get('in_yesterday_limit_pool') else '否'} |")
    ranked_turns = sorted(turns, key=lambda p: (-(float(p.get("window_amount_yuan") or 0) * float(p.get("amplitude") or 0)), p["timestamp"]))[:30]
    lines.extend(["", "## 转折点 Top30", "", "按窗口额×转折幅度排序，仅用于展示，不是评分。"])
    lines.extend(f"- {p['timestamp']} {p['object_type']}:{p['object']} {p['kind']} {p.get('before')} → {p.get('value')} → {p.get('after')}" for p in ranked_turns)
    return "\n".join(lines) + "\n"
