"""Windowed auction/opening bars and rule-based turning points.

All thresholds are review prompts and remain explicitly uncalibrated.
"""
from __future__ import annotations

import html
import statistics
from collections import defaultdict
from bisect import bisect_right
from typing import Any, Iterable, Mapping

from .auction_tracking import Tick, _at_limit, _seconds, _stamp, derive_state

WINDOWS_MINUTES = (1, 2, 5, 15)
PEAK_MIN_AMOUNT_YUAN = 1_000_000  # 待校准
PEAK_MIN_CHANGE_PP = 0.5  # 待校准
ACCELERATION_MULTIPLE = 2.0  # 待校准
FLIP_MIN_CHANGE_PP = 0.5  # 待校准
PLATE_SHARE_FLIP_BP = 50  # 待校准
LATE_RUSH_SHARE_PCT = 40.0  # 待校准
EARLY_FAVORITE_BEFORE = "09:22:00"  # 待校准
FADE_AMOUNT_MULTIPLE = 0.5  # 待校准


def _price_pct(price: int, close: int) -> float | None:
    return (price / close - 1) * 100 if price > 0 and close > 0 else None


def detect_turning_points(rows: list[Mapping[str, Any]], *, flip_min: float = FLIP_MIN_CHANGE_PP,
                         peak_min: float | None = None) -> list[dict[str, Any]]:
    """Detect material local amount extrema, sustained acceleration, and pct flips."""
    if peak_min is None:
        peak_min = PEAK_MIN_AMOUNT_YUAN
    points: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        amount = row.get("window_amount_yuan")
        if i >= 2 and i + 1 < len(rows) and amount is not None:
            before = rows[i - 1].get("window_amount_yuan")
            after = rows[i + 1].get("window_amount_yuan")
            if before is not None and after is not None:
                peak = amount > before and amount >= after and amount - max(before, after) >= peak_min
                valley = amount < before and amount <= after and min(before, after) - amount >= peak_min
                if peak or valley:
                    points.append({"kind": "AMOUNT_PEAK" if peak else "AMOUNT_VALLEY",
                        "timestamp": row["timestamp"], "before": before, "value": amount, "after": after,
                        "window_minutes": row.get("window_minutes")})
        if i >= 2:
            prev2, prev1 = rows[i - 2].get("window_amount_yuan"), rows[i - 1].get("window_amount_yuan")
            cur = amount
            if prev2 is not None and prev1 is not None and cur is not None and prev2 > 0 and prev1 > 0:
                first = prev1 / prev2
                second = cur / prev1
                if first >= ACCELERATION_MULTIPLE and second >= ACCELERATION_MULTIPLE:
                    points.append({"kind": "ACCELERATION_START", "timestamp": rows[i - 1]["timestamp"],
                        "before": prev2, "value": prev1, "after": cur,
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
                if flipped and abs(current_pct - before_pct) >= flip_min:
                    points.append({"kind": "DIRECTION_FLIP", "timestamp": row["timestamp"],
                        "before": before_pct, "value": current_pct, "after": current_pct,
                        "window_minutes": row.get("window_minutes")})
    return points


def summarize_curve(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
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
    labels = []
    if last_share is not None and last_share >= LATE_RUSH_SHARE_PCT:
        labels.append("LATE_RUSH")
    if t50 and t50[-8:] < EARLY_FAVORITE_BEFORE:
        labels.append("EARLY_FAVORITE")
    flips = sum(p["kind"] == "DIRECTION_FLIP" for p in detect_turning_points(auction))
    if flips >= 2:
        labels.append("REVERSAL")
    return {"t50_timestamp": t50, "last_60s_share_pct": round(last_share, 2) if last_share is not None else None,
            "amount_peak_timestamp": next((p["timestamp"] for p in detect_turning_points(auction) if p["kind"] == "AMOUNT_PEAK"), None),
            "direction_flip_count": flips, "labels": labels, "terminal_amount_yuan": terminal}


def build_bar_report(*, trade_date: str, auction_ticks: Iterable[Tick], open_ticks: Iterable[Tick] = (),
                     relations: Mapping[str, set[str]] | None = None, plate_names: Mapping[str, str] | None = None,
                     generic_names: set[str] | None = None, pool_symbols: set[str] | None = None,
                     prev_close: Mapping[str, Any] | None = None,
                     baseline: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build compact market/plate/focus-stock rolling bars and report fragments."""
    relations, plate_names, generic_names = relations or {}, plate_names or {}, generic_names or set()
    auction = sorted((t for t in auction_ticks if t.trade_date == trade_date), key=lambda t: (_seconds(t.ts), t.symbol))
    opened = sorted((t for t in open_ticks if t.trade_date == trade_date), key=lambda t: (_seconds(t.ts), t.symbol))
    state: dict[str, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []
    ai = 0
    for sec in range(_seconds("09:15:00"), _seconds("09:25:00") + 1, 30):
        while ai < len(auction) and _seconds(auction[ai].ts) <= sec:
            state[auction[ai].symbol] = derive_state(auction[ai]); ai += 1
        samples.append({"sec": sec, "phase": "AUCTION", "states": {k: dict(v) for k, v in state.items()}})
    auction_anchor = {k: dict(v) for k, v in state.items()}
    oi = 0
    for sec in range(_seconds("09:30:00"), _seconds("09:45:00") + 1, 60):
        while oi < len(opened) and _seconds(opened[oi].ts) <= sec:
            tick = opened[oi]
            row = derive_state(tick)
            row["match_amount_yuan"] = max(0, int(tick.amt_yuan))
            state[tick.symbol] = row
            oi += 1
        samples.append({"sec": sec, "phase": "OPEN", "states": {k: dict(v) for k, v in state.items()}})
    terminal = samples[20]["states"] if len(samples) > 20 else (samples[-1]["states"] if samples else {})
    stock_amounts = [(s, int(v.get("match_amount_yuan") or 0)) for s, v in terminal.items()]
    pool_symbols = set(pool_symbols or ())
    ranked_symbols = [s for s, _ in sorted(stock_amounts, key=lambda x: (-x[1], x[0]))]
    top50_symbols = set(ranked_symbols[:50])
    focus = pool_symbols | set(ranked_symbols[:200])
    prev_close = prev_close or {}
    for snap in samples:
        for symbol, row in snap["states"].items():
            close = row.get("prev_close_milli") or prev_close.get(symbol)
            if isinstance(close, Mapping): close = close.get("prev_close_milli", close.get("pc_milli"))
            row["pct_change"] = _price_pct(int(row.get("price_milli") or 0), int(close or 0))
    # Build point-in-time aggregates once per sample. Missing inputs stay null.
    aggregates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for snap in samples:
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
        for code, members in relations.items():
            name = plate_names.get(code, code)
            if name in generic_names: continue
            selected = [states[s] for s in members if s in states and states[s].get("pct_change") is not None]
            if len(selected) < 5: continue
            aggregates[f"plate:{code}"].append({"sec": snap["sec"], "phase": snap["phase"],
                "cum_amount": sum(int(v.get("match_amount_yuan") or 0) for v in selected),
                "pct": statistics.mean(v["pct_change"] for v in selected),
                "up": sum(_at_limit(v, "up") for v in selected),
                "down": sum(_at_limit(v, "down") for v in selected),
                "final_count": sum(v.get("status") == "FINAL" for v in selected),
                "indicative_count": sum(v.get("status") == "INDICATIVE" for v in selected)})
        for symbol in focus:
            v = states.get(symbol)
            if not v: continue
            aggregates[f"stock:{symbol}"].append({"sec": snap["sec"], "phase": snap["phase"],
                "cum_amount": v.get("match_amount_yuan"), "pct": v.get("pct_change"),
                "price": v.get("price_milli"), "auction_price": auction_anchor.get(symbol, {}).get("price_milli"),
                "up": int(_at_limit(v, "up")), "down": int(_at_limit(v, "down"))})
    bars: list[dict[str, Any]] = []
    curves: dict[str, list[dict[str, Any]]] = {}
    market_points = {(x["phase"], x["sec"]): x for x in aggregates["market"]}
    for key, curve in aggregates.items():
        curves[key] = []
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
                    "amount_multiple": None, "end_pct": cur["pct"], "end_price_milli": cur.get("price"),
                    "limit_up_count": cur["up"], "limit_down_count": cur["down"],
                    "final_count": cur.get("final_count"), "indicative_count": cur.get("indicative_count"),
                    "cumulative_amount_yuan": cur["cum_amount"]}
                prev_amount = prior_amount.get(cur["phase"])
                if amount is not None and prev_amount:
                    row["amount_multiple"] = round(amount / prev_amount, 3)
                if pct is not None:
                    row["window_pct_change_pp"] = round(pct, 6)
                prior_amount[cur["phase"]] = amount
                bars.append(row); curves[key].append(row)
    turns = []
    for key, rows in curves.items():
        for window in WINDOWS_MINUTES:
            for phase in ("AUCTION", "OPEN"):
                selected = [r for r in rows if r["window_minutes"] == window and r["phase"] == phase]
                for point in detect_turning_points(selected):
                    point.update({"object_type": key.split(":")[0], "object": key.split(":", 1)[1] if ":" in key else "ALL"})
                    turns.append(point)
                if key.startswith("plate:"):
                    share_rows = [{**r, "window_amount_yuan": r["market_share_bp"],
                                   "window_pct_change_pp": r["market_share_bp"]}
                                  for r in selected if r.get("market_share_bp") is not None]
                    for point in detect_turning_points(share_rows, flip_min=PLATE_SHARE_FLIP_BP,
                                                       peak_min=PLATE_SHARE_FLIP_BP):
                        point["kind"] = "SHARE_" + point["kind"]
                        point["series"] = "market_share_bp"
                        point.update({"object_type": "plate", "object": key.split(":", 1)[1]})
                        turns.append(point)
    summaries = []
    for key, curve in aggregates.items():
        if key.startswith("stock:") and key.split(":", 1)[1] not in focus: continue
        summary = summarize_curve([{"timestamp": _stamp(trade_date, x["sec"]), "phase": x["phase"],
            "cumulative_amount_yuan": x["cum_amount"], "window_amount_yuan": None,
            "window_pct_change_pp": x["pct"]} for x in curve])
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
                    "amount_multiple": item["amount_multiple"], "pct_change_pp": item["window_pct_change_pp"]}
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
        summaries.append({"object_type": key.split(":")[0], "object": key.split(":", 1)[1] if ":" in key else "ALL", **summary})
    baseline = baseline or {}
    prior_days = sorted(str(day) for day in baseline if str(day) < trade_date)
    baseline_amount = int(baseline.get(prior_days[-1]) or 0) if prior_days else None
    html_report = _render_html(trade_date, bars, summaries, turns, plate_names, baseline_amount)
    md = _render_markdown(trade_date, summaries, turns)
    return {"trade_date": trade_date, "bars": bars, "turning_points": turns, "summaries": summaries,
            "html": html_report, "markdown": md, "focus_symbols": sorted(focus),
            "previous_day_terminal_amount_yuan": baseline_amount}


def _render_html(trade_date: str, bars: list[dict[str, Any]], summaries: list[dict[str, Any]],
                 turns: list[dict[str, Any]], names: Mapping[str, str], baseline_amount: int | None) -> str:
    plates = sorted((r for r in summaries if r["object_type"] == "plate"),
                    key=lambda r: (-(r.get("share_0925_bp") or 0), r["object"]))[:20]
    stocks = [r for r in summaries if r["object_type"] == "stock" and
              (r.get("in_yesterday_limit_pool") or r.get("sealed_0925") or r.get("top50_auction_amount"))]
    chosen = [r for r in summaries if r["object_type"] == "market"] + plates + stocks
    sections = []
    for item in chosen:
        obj = html.escape(names.get(item["object"], item["object"]))
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
        f"<h1>大盘额曲线 · {trade_date}</h1><p>竞价虚拟撮合；09:25:00 起使用 FINAL px/amt_yuan。{baseline_note}橙点标注额峰/谷或加速。窗口额为累计额增量；窗口涨幅为端点变化，板块按等权。阈值待校准: 峰谷差≥{PEAK_MIN_AMOUNT_YUAN:,}元，翻转幅度≥{FLIP_MIN_CHANGE_PP}pp，加速倍数≥{ACCELERATION_MULTIPLE}且连续2柱，LATE_RUSH≥{LATE_RUSH_SHARE_PCT}%。q2 spd1m/amt2m/amt5m 分别对应 1/2/5 分钟字段，历史值由 tick 重建。</p><main>{''.join(sections)}</main></html>")


def _render_markdown(trade_date: str, summaries: list[dict[str, Any]], turns: list[dict[str, Any]]) -> str:
    lines = [f"# 窗口指标 {trade_date}", "", f"竞价阶段为虚拟撮合；09:25:00 起 px/amt_yuan 为 FINAL，之前价格为 INDICATIVE。窗口额为累计额增量，板块涨幅为等权端点变化。q2 spd1m/amt2m/amt5m 分别对应 1/2/5 分钟字段；本报告历史窗口由 TD tick 重建。规则阈值待校准: 峰谷差≥{PEAK_MIN_AMOUNT_YUAN:,}元、涨幅翻转≥{FLIP_MIN_CHANGE_PP}pp、板块份额翻转≥{PLATE_SHARE_FLIP_BP}bp、加速≥{ACCELERATION_MULTIPLE}连续2柱、LATE_RUSH≥{LATE_RUSH_SHARE_PCT}%。缺失值不补零。", "", "| 类型 | 对象 | t50 | 最后60秒占比 | 额峰 | 0920→0925份额变化(bp) | 开盘 1m / 5m / 15m (额倍数,涨幅pp) | 首次跌破 / 站回0925 | 方向翻转 | 标签 |", "|---|---|---|---:|---|---:|---|---|---:|---|"]
    for r in summaries:
        opening = r.get("opening_windows", {})
        opening_text = " / ".join(f"{opening.get(str(w), {}).get('amount_multiple', '缺失')},{opening.get(str(w), {}).get('pct_change_pp', '缺失')}" for w in (1, 5, 15))
        crossing = f"{r.get('first_below_auction_timestamp') or '缺失'} / {r.get('first_above_auction_timestamp') or '缺失'}" if r["object_type"] == "stock" else "—"
        share_delta = r.get("share_change_0920_0925_bp", "—")
        lines.append(f"| {r['object_type']} | {r['object']} | {r.get('t50_timestamp') or '缺失'} | {r.get('last_60s_share_pct') if r.get('last_60s_share_pct') is not None else '缺失'} | {r.get('amount_peak_timestamp') or '缺失'} | {share_delta} | {opening_text} | {crossing} | {r['direction_flip_count']} | {', '.join(r['labels']) or '—'} |")
    lines.extend(["", "## 转折点", ""])
    lines.extend(f"- {p['timestamp']} {p['object_type']}:{p['object']} {p['kind']} {p.get('before')} → {p.get('value')} → {p.get('after')}" for p in turns)
    return "\n".join(lines) + "\n"
