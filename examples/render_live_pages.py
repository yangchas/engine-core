#!/usr/bin/env python3
"""Render a self-contained, read-only auction or opening status page from injected TD."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import statistics
import time
from datetime import datetime
from pathlib import Path

from engine_core.live_session import (market_price_volume_status, build_live_slice, checkpoint_ready, daily_history_context,
                                      load_auction_amount_baseline_csv, auction_amount_baseline_comparison,
                                      load_daily_history_csv, states_at)
from engine_core.plate_enrichment import infer_plate_enrichment
from engine_core.plate_linkage import build_plate_linkage

STAGES = {"auction_0926": [("0920", "09:20:00"), ("0924", "09:24:00"), ("0925", "09:25:06")],
          "open_0946": [("0925", "09:25:06"), ("0932", "09:32:00"), ("0935", "09:35:00"),
                        ("0940", "09:40:00"), ("0945", "09:45:30")]}
SECTIONS = {"auction_0926": ["一屏结论", "大盘", "板块份额", "板块象限", "板块联动与跌停影响", "封涨停质量", "封跌停质量", "昨日连板高标", "竞价内变化", "看A做B", "近72日对比"],
            "open_0946": ["一屏结论", "大盘", "板块象限", "昨日连板高标分化", "昨日涨停池", "竞价后新冒头", "板块回升榜", "板块回落榜", "板块联动与跌停影响", "封涨停质量", "封跌停质量", "竞价内变化", "看A做B", "近72日对比"]}
GENERIC_NAMES_PATH = Path(__file__).resolve().parent / "data" / "plate_generic_factor_names.json"
EVENT_NAME_MARKERS = ("季报增长", "年报增长", "中报增长", "并购重组", "实控人变更")


def _rows(states, top=20):
    return sorted((dict(row) for row in states.values()),
                  key=lambda row: (-(int(row.get("match_amount_yuan") or 0)), row.get("symbol", "")))[:top]


def _input_manifest(value, kind):
    if not value: return {"provided": False, "sha256": None, "captured_local": None}
    path = Path(value)
    raw = path.read_bytes()
    captured = None
    if kind in ("kaipan_hot", "strength_anchor", "amount_baseline"):
        try:
            data = json.loads(raw)
            captured = data.get("captured_local") if isinstance(data, dict) else None
            if captured is None and kind == "strength_anchor" and isinstance(data, dict):
                captured = next((row.get("ts") for row in data.values()
                                 if isinstance(row, dict) and row.get("ts")), None)
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    return {"provided": True, "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
            "captured_local": captured}


def _auction_baseline_manifest(value, rows):
    if not value:
        return {"provided": False, "sha256": None, "sample_days": 0,
                "earliest_date": None, "latest_date": None}
    try:
        raw = Path(value).read_bytes()
        return {"provided": True, "path": str(value),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "sample_days": len(rows or []),
                "earliest_date": min((row["date"] for row in rows or []), default=None),
                "latest_date": max((row["date"] for row in rows or []), default=None)}
    except OSError:
        return {"provided": False, "path": str(value), "sha256": None, "sample_days": 0,
                "earliest_date": None, "latest_date": None}


def _generic_factor_names():
    if not GENERIC_NAMES_PATH.exists(): return set()
    data = json.loads(GENERIC_NAMES_PATH.read_text(encoding="utf-8"))
    return set(data if isinstance(data, list) else data.get("names", []))


def _plate_linkage_chapter(raw_states, stage, relations, plate_names, stock_names):
    generic_names = _generic_factor_names() | set(EVENT_NAME_MARKERS)
    current_label = "0945" if stage == "open_0946" else "0925"
    current = build_plate_linkage(raw_states.get(current_label, {}), relations, plate_names,
                                  stock_names=stock_names, generic_names=generic_names,
                                  min_members=8, top_n=None)
    all_rows = [row for row in current["rows"]
                if not any(marker in row["name"] for marker in EVENT_NAME_MARKERS)]
    displayed = all_rows[:15]
    changes = None
    if stage == "open_0946":
        earlier = build_plate_linkage(raw_states.get("0925", {}), relations, plate_names,
                                      stock_names=stock_names, generic_names=generic_names,
                                      min_members=8, top_n=None)
        before = {row["code"]: row for row in earlier["rows"]
                  if not any(marker in row["name"] for marker in EVENT_NAME_MARKERS)}
        change_fields = ("up_share_pct", "equal_weight_median_pct", "limit_up_count",
                         "limit_up_amount_share_pct", "limit_down_count",
                         "limit_down_amount_share_pct")
        changes = []
        for row in displayed:
            old = before.get(row["code"])
            deltas = {field: (row[field] - old[field]
                              if old and row[field] is not None and old[field] is not None else None)
                      for field in change_fields}
            changes.append({"code": row["code"], "name": row["name"], "delta": deltas})
    return {"available": bool(displayed), "slice_label": current_label,
            "comparison_label": "0925" if stage == "open_0946" else None,
            "rows": displayed, "down_limit_top5": current["down_limit_top5"],
            "opening_changes": changes, "method_note": current["method_note"],
            "description_only": True}


def _relation_snapshot_date(relation_dir):
    if not relation_dir: return "UNKNOWN"
    path = Path(relation_dir)
    registry_path = path / "registry.json"
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            value = next((data[key] for key in ("snapshot_date", "as_of", "asof", "trade_date")
                          if data.get(key)), None)
            if value: return f"PIT {str(value).replace('-', '')[:8]}"
    except (OSError, ValueError):
        pass
    match = re.search(r"20\d{6}", str(path))
    return f"PIT {match.group(0)}" if match else "UNKNOWN"


def _load_amount_baseline(value):
    if not value: return None
    data = json.loads(Path(value).read_text(encoding="utf-8"))
    if isinstance(data, (int, float)): amount = data
    elif isinstance(data, dict):
        amount = data.get("amount_yuan", data.get("baseline_yuan", data.get("market_match_amount_yuan")))
    else: amount = None
    return float(amount) if isinstance(amount, (int, float)) and amount > 0 else None


def _plate_quadrants(current, relations, plate_names, hot_path, anchor_path, now):
    plate_stats = {row["name"]: row for row in current.get("top_plates_by_share", [])}
    plate_stats_by_code = {row["code"]: row for row in current.get("top_plates_by_share", [])}
    equal_weight = sorted(plate_stats.values(), key=lambda row: (-row["equal_weight_pct"], row["name"]))[:15]
    amount_down = sorted((row for row in plate_stats.values() if row["equal_weight_pct"] < 0),
                         key=lambda row: (-row["match_amount_yuan"], row["name"]))[:10]
    block = {"available": False, "status": "Kaipan 强度不可用；未提供抓取输入",
             "captured_local": None, "counts": {"双正": 0, "双负": 0, "强度正净流出背离": 0,
                                                   "强度负净流入背离": 0, "零值边界": 0},
             "rows": [], "fallback_equal_weight": equal_weight, "fallback_amount_down": amount_down,
             "fallback_note": "等权涨幅与金额榜只作事实降级；金额榜的净流出为价格与金额推断。",
             "strength_estimates": [], "display_rows": [], "zero_value_boundary_count": 0,
             "description_only": True}
    if anchor_path:
        anchors = json.loads(Path(anchor_path).read_text(encoding="utf-8"))
        for row in equal_weight:
            anchor = anchors.get(row["name"], anchors.get(row["code"])) if isinstance(anchors, dict) else None
            if not isinstance(anchor, dict): continue
            try:
                anchor_ts = datetime.fromisoformat(str(anchor["ts"]).replace(" ", "T"))
                if anchor_ts.date() != now.date() or anchor_ts > now: continue
                age = int((now - anchor_ts).total_seconds())
                estimate = float(anchor["strength"]) + 100.0 * (float(row["equal_weight_pct"]) - float(anchor["pct"]))
            except (KeyError, TypeError, ValueError):
                continue
            block["strength_estimates"].append({"name": row["name"], "estimated_strength": estimate,
                                                "anchor_ts": anchor_ts.isoformat(timespec="seconds"),
                                                "anchor_age_seconds": age,
                                                "method": "anchor_strength + 100 × (our_pct - anchor_pct)"})
    if not hot_path:
        return block
    data = json.loads(Path(hot_path).read_text(encoding="utf-8"))
    captured = data.get("captured_local") if isinstance(data, dict) else None
    response = data.get("response", {}) if isinstance(data, dict) else {}
    raw_rows = response.get("list", []) if isinstance(response, dict) else []
    parsed = []
    for item in raw_rows:
        if not isinstance(item, (list, tuple)) or len(item) < 9: continue
        try:
            strength, hot_pct, amount, net = float(item[2]), float(item[3]), float(item[5]), float(item[6])
        except (TypeError, ValueError): continue
        parsed.append({"code": str(item[0]), "name": str(item[1]), "strength": strength,
                       "kaipan_pct": hot_pct, "amount_yuan": amount, "net_yuan": net})
    if not parsed or all(row["strength"] == 0 and row["net_yuan"] == 0 for row in parsed):
        block["status"] = "Kaipan 强度不可用；抓取动态字段全零"
        block["captured_local"] = captured
        return block
    generic_data = json.loads(GENERIC_NAMES_PATH.read_text(encoding="utf-8"))
    generic_names = set(generic_data.get("names", []))
    rows = []
    counts = dict(block["counts"])
    for hot in parsed:
        if hot["amount_yuan"] < 2_000_000_000: continue
        ours = plate_stats_by_code.get(hot["code"]) or plate_stats.get(hot["name"])
        our_pct = ours.get("equal_weight_pct") if ours else None
        if hot["strength"] == 0 or hot["net_yuan"] == 0: quadrant = "零值边界"
        elif hot["strength"] > 0 and hot["net_yuan"] > 0: quadrant = "双正"
        elif hot["strength"] < 0 and hot["net_yuan"] < 0: quadrant = "双负"
        elif hot["strength"] > 0: quadrant = "强度正净流出背离"
        else: quadrant = "强度负净流入背离"
        counts[quadrant] += 1
        nominal = len(relations.get(ours["code"], ())) if ours else None
        coverage = ours["member_count"] / nominal if ours and nominal else None
        flags = []
        if hot["name"] in generic_names: flags.append("泛化名单")
        if any(marker in hot["name"] for marker in EVENT_NAME_MARKERS): flags.append("事件类名提示")
        if nominal is not None and nominal <= 15: flags.append("名义成员<=15")
        if coverage is not None and coverage < .5: flags.append("有效价格覆盖<50%")
        rows.append({**hot, "quadrant": quadrant, "our_pct": our_pct,
                     "pct_diff_pp": (our_pct - hot["kaipan_pct"]) if our_pct is not None else None,
                     "member_count": nominal, "effective_price_coverage": coverage,
                     "flags": flags})
    double_positive = [row for row in rows if row["quadrant"] == "双正"]
    divergences = [row for row in rows if row["quadrant"] in ("强度正净流出背离", "强度负净流入背离")]
    double_negative = sorted((row for row in rows if row["quadrant"] == "双负"),
                             key=lambda row: (row["net_yuan"], row["name"]))[:15]
    display_rows = double_positive + divergences + double_negative
    block.update({"available": True, "status": "可用；只作描述，不表示因果或胜负",
                  "captured_local": captured, "counts": counts, "rows": rows,
                  "display_rows": display_rows, "zero_value_boundary_count": counts["零值边界"]})
    return block


def render_page(td, *, trade_date, stage, previous_day_final_yuan=None,
                relations=None, plate_names=None, daily_history_csv=None, stock_names=None,
                amount_baseline_json=None, auction_baseline_csv=None, kaipan_hot_json=None, strength_anchor_json=None,
                member_snapshot_date="UNKNOWN"):
    if stage not in STAGES: raise ValueError(f"unknown stage: {stage}")
    snaps, raw_states = [], {}
    td_fetch_seconds = 0.0
    aggregation_started = time.perf_counter()
    baseline_yuan = _load_amount_baseline(amount_baseline_json)
    auction_baseline_rows = load_auction_amount_baseline_csv(auction_baseline_csv, trade_date)
    for label, clock in STAGES[stage]:
        cutoff = datetime.fromisoformat(f"{trade_date}T{clock}")
        fetch_started = time.perf_counter()
        if clock == "09:45:30" and not checkpoint_ready(td, cutoff):
            snaps.append({"label": label, "asof": cutoff.isoformat(timespec="seconds"),
                          "status": "NOT_READY", "data_lag_seconds": None,
                          "market_match_amount_yuan": None, "vs_previous_day_final": None,
                          "median_pct_change": None, "up_share_pct": None, "limit_up": None,
                          "limit_down": None, "top_plates_by_share": [], "top_stocks_by_amount": []})
            td_fetch_seconds += time.perf_counter() - fetch_started
            continue
        rows = states_at(td, cutoff)
        td_fetch_seconds += time.perf_counter() - fetch_started
        raw_states[label] = rows
        snap = build_live_slice(rows, cutoff, previous_day_final_yuan=previous_day_final_yuan,
                                relations=relations, plate_names=plate_names, top_n=max(15, len(relations or {})))
        snap["amount_baseline_yuan"] = baseline_yuan
        snap["vs_amount_baseline"] = (snap["market_match_amount_yuan"] / baseline_yuan
                                      if baseline_yuan and snap["market_match_amount_yuan"] else None)
        snap["label"] = label
        comparison = None
        if stage == "auction_0926" and label == "0925":
            total_yi = (snap.get("market_match_amount_yuan") / 100_000_000
                        if snap.get("market_match_amount_yuan") is not None else None)
            comparison = auction_amount_baseline_comparison(auction_baseline_rows, trade_date, total_yi)
        snap["auction_baseline_comparison"] = comparison
        if clock > "09:26:00":
            # 0926 之后的 TD 额是当日累计成交额, 不能与昨日 0925 竞价终值相比
            snap["vs_previous_day_final"] = None
            snap["market_price_volume_state"] = market_price_volume_status(
                None, snap.get("amount_weighted_pct_change"), snap.get("up_share_pct"))
            snap["amount_basis"] = "cumulative_session"
        snap["top_stocks_by_amount"] = _rows(rows)
        snaps.append(snap)
    sections = SECTIONS[stage]
    relations, plate_names, stock_names = relations or {}, plate_names or {}, stock_names or {}
    final_label = "0945" if stage == "open_0946" else "0925"
    current = raw_states.get(final_label, {})
    history_rows = load_daily_history_csv(daily_history_csv) if daily_history_csv else None
    context = daily_history_context(history_rows, trade_date, raw_states.get("0925", current)) if history_rows is not None else None
    stock_context = context["stocks"] if context else {}
    history = context["history"] if context else None
    chapters = _build_chapters(snaps, raw_states, stage, relations, plate_names,
                               stock_names, stock_context, history, history_rows is not None)
    chapters["plate_linkage"] = _plate_linkage_chapter(
        raw_states, stage, relations, plate_names, stock_names)
    chapters["plate_enrichment"] = {"status": "仅开盘更新页面计算；当前阶段不适用",
                                    "emerging_count": 0, "N": 0,
                                    "member_snapshot_date": member_snapshot_date,
                                    "plates": [], "method_note": "仅称关联不称买点"}
    if stage == "open_0946":
        chapters["plate_enrichment"] = infer_plate_enrichment(
            raw_states.get("0925", {}), raw_states.get("0945", {}), relations,
            plate_names=plate_names, stock_names=stock_names,
            generic_names=_generic_factor_names(), member_snapshot_date=member_snapshot_date)
        enrichment = chapters["plate_enrichment"]
        if len(enrichment.get("plates", [])) > 15:
            enrichment["plates_total"] = len(enrichment["plates"])
            enrichment["plates"] = enrichment["plates"][:15]
    report_current = next((snap for snap in reversed(snaps) if snap.get("status") != "NOT_READY"), {})
    chapters["market_price_volume"] = report_current
    opening_snap = next((snap for snap in snaps if snap.get("label") == "0932"), None)
    chapters["market_price_volume"]["opening_median_pct_change"] = (
        opening_snap.get("median_pct_change") if opening_snap else None)
    chapters["plate_quadrants"] = _plate_quadrants(
        report_current, relations, plate_names, kaipan_hot_json, strength_anchor_json,
        datetime.fromisoformat(report_current.get("asof", f"{trade_date}T09:25:06")))
    conclusion = _conclusion(snaps, chapters, history, stage)
    aggregation_seconds = max(0.0, time.perf_counter() - aggregation_started - td_fetch_seconds)
    limitations = "板块关系使用调用方提供的点时关系；名称来自可选名称表或代码。日线输入缺失时，昨日连板、A/B位置与72日比较明确降级；缺失字段不补零。09:45 切片未追平时标记 NOT_READY/STALE。"
    manifest = {"inputs": {"kaipan_hot": _input_manifest(kaipan_hot_json, "kaipan_hot"),
                            "strength_anchor": _input_manifest(strength_anchor_json, "strength_anchor"),
                            "amount_baseline": _input_manifest(amount_baseline_json, "amount_baseline"),
                            "auction_baseline": _auction_baseline_manifest(auction_baseline_csv,
                                                                            auction_baseline_rows)}}
    payload = {"trade_date": trade_date, "stage": stage, "snapshots": snaps, "sections": sections,
               "conclusion": conclusion, "history": history, "chapters": chapters,
               "manifest": manifest,
               "limitations": limitations,
               "method": {"source": "TD stock_tick_v2 (read-only)",
                          "auction_price": "09:25:00 前用有效一档匹配价；09:25:00 起用 px_milli 和 amt_yuan 终值",
                          "amount": "竞价期为虚拟匹配额；开盘后为 TD 成交额累计值",
                          "missing": "缺字段保持 null，不补 0",
                          "limitations": limitations}}
    render_started = time.perf_counter()
    content = "".join(f"<section><h2>{html.escape(title)}</h2>{_section_body(title, payload)}</section>" for title in sections)
    safe_limitations = html.escape(limitations)
    page = f'''<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{trade_date} {stage}</title><style>
:root{{color-scheme:light dark;--bg:#f4f6fa;--fg:#172033;--panel:#fff;--accent:#2563eb;--muted:#667085}}
@media(prefers-color-scheme:dark){{:root{{--bg:#111827;--fg:#e5e7eb;--panel:#1f2937;--accent:#60a5fa;--muted:#aab4c3}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,sans-serif}}main{{max-width:1100px;margin:auto;padding:20px}}h1{{font-size:1.55rem}}section{{background:var(--panel);border-radius:12px;padding:16px;margin:14px 0;overflow:auto}}h2{{font-size:1.2rem;margin:0 0 12px}}table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}td,th{{text-align:left;padding:7px;border-bottom:1px solid #8892a033}}small,.muted{{color:var(--muted)}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:9px}}.card{{padding:10px;background:var(--bg);border-radius:8px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}</style></head>
<body><main><h1>{trade_date} · {stage}</h1><p class="muted">只读观测；来源、口径与局限见页面末尾。状态值按可用数据计算。</p>{content}
<section><h2>口径与局限</h2><p>{safe_limitations}</p></section></main></body></html>'''
    render_seconds = time.perf_counter() - render_started
    payload["manifest"]["timings_ms"] = {"td_fetch": round(td_fetch_seconds * 1000, 3),
                                         "aggregation": round(aggregation_seconds * 1000, 3),
                                         "render": round(render_seconds * 1000, 3)}
    return page, payload


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _state_limit(row, side):
    state = row.get("limit_state")
    if state in (-1, 1): return state == side
    from engine_core.auction_tracking import _at_limit
    return _at_limit(row, "up" if side == 1 else "down")


def _build_chapters(snaps, raw_states, stage, relations, plate_names, stock_names,
                    stock_context, history, has_history):
    from engine_core.behavior_tags import calculate_stock_tags, summarize
    labels = {snap["label"]: snap for snap in snaps}
    auction = raw_states.get("0925", {})
    last_label = "0945" if stage == "open_0946" else "0925"
    latest = raw_states.get(last_label, {})
    chapters = {"yesterday_highboards": "缺少输入" if not has_history else [],
                "look_a_do_b": "缺少输入" if not has_history else [],
                "history_comparison": "缺少输入" if history is None else history,
                "behavior_tags": {}, "seal_quality": {}, "sealed_up": [], "sealed_down": [],
                "emerging": [], "plate_changes": {"up": [], "down": []}, "yesterday_limit_pool": []}
    if not auction:
        chapters["behavior_tags"] = {"direction_counts": {}, "seal_quality": {}}
        chapters["seal_quality"] = {"up_count": 0, "down_count": 0, "up_median_yuan": None, "down_median_yuan": None}
        chapters["behavior_rows"] = []
        chapters["watchlist"] = {"amount_top50": [], "yesterday_limit_pool": [], "sealed_up": [],
                                 "sealed_down": [], "behavior_candidates": []}
        return chapters
    # C155 tags use the canonical 0920/0924/0925 values and market totals.
    market = {tag: {"m": sum(int(row.get("match_amount_yuan") or 0) for row in raw_states.get(tag, {}).values())}
              for tag in ("0920", "0924", "0925")}
    plate_pct, plate_means, symbol_plates = {}, {}, {}
    auction_state = raw_states.get("0925", {})
    for code, members in relations.items():
        values = []
        for symbol in members:
            symbol_plates.setdefault(symbol, []).append(code)
            row = auction_state.get(symbol)
            if row is not None and row.get("pct_change") is not None:
                values.append(row["pct_change"])
        if values:
            plate_means[code] = statistics.mean(values)
            if len(values) >= 5: plate_pct[code] = plate_means[code]
    tagged = {}
    for symbol in set().union(*(set(raw_states.get(tag, {})) for tag in ("0920", "0924", "0925"))):
        stock = {"symbol": symbol, "prev_day_amount": stock_context.get(symbol, {}).get("prev_day_amount_yuan"),
                 "plates_0925": {code: plate_pct[code] for code in symbol_plates.get(symbol, ())
                                 if code in plate_pct}}
        for tag in ("0920", "0924", "0925"):
            row = raw_states.get(tag, {}).get(symbol)
            if row is None: continue
            stock[tag] = {"pct": row.get("pct_change"), "m": row.get("match_amount_yuan"),
                          "rb": row.get("rest_bid_yuan"), "ra": row.get("rest_ask_yuan"),
                          "limit_state": 1 if _state_limit(row, 1) else -1 if _state_limit(row, -1) else 0,
                          "status": row.get("status")}
        tagged[symbol] = {**stock, **calculate_stock_tags(stock, market)}
    chapters["behavior_tags"] = summarize(market, tagged)
    chapters["behavior_rows"] = [{"symbol": symbol, "name": stock_names.get(symbol, symbol),
                                  "display_tag": (tag.get("direction") or "") + ("；抢跑候选" if tag.get("outrun") else ""),
                                  **tag}
                                  for symbol, tag in sorted(tagged.items(), key=lambda pair: -(pair[1].get("share_0925") or 0))
                                  if tag.get("direction") or tag.get("outrun")]
    sealed_up, sealed_down = [], []
    for symbol, row in auction.items():
        later = latest.get(symbol, {})
        latest_side = 1 if later and _state_limit(later, 1) else -1 if later and _state_limit(later, -1) else 0
        earlier = [raw_states.get(tag, {}).get(symbol, {}) for tag in ("0925", "0932", "0935", "0940")]
        was_sealed = _state_limit(row, 1)
        result_status = ("封死" if latest_side == 1 and all(_state_limit(x, 1) for x in earlier if x)
                         else "回封" if latest_side == 1 else "开板" if was_sealed and latest_side != 1
                         else "封跌停" if latest_side == -1 else "观察")
        item = {"symbol": symbol, "name": stock_names.get(symbol, row.get("name") or symbol),
                "pct_change": row.get("pct_change"), "match_amount_yuan": row.get("match_amount_yuan"),
                "seal_yuan": row.get("rest_bid_yuan") if _state_limit(row, 1) else row.get("rest_ask_yuan"),
                "streak": stock_context.get(symbol, {}).get("streak"),
                "current_pct": later.get("pct_change"), "status": result_status}
        if _state_limit(row, 1): sealed_up.append(item)
        if _state_limit(row, -1): sealed_down.append(item)
    chapters["sealed_up"] = sorted(sealed_up, key=lambda row: -(row.get("seal_yuan") or 0))
    chapters["sealed_down"] = sorted(sealed_down, key=lambda row: -(row.get("seal_yuan") or 0))
    amount_top50 = _rows(auction, 50)
    amount_top50 = [{"symbol": row["symbol"],
                     "name": stock_names.get(row["symbol"], row.get("name") or row["symbol"]),
                     "pct_change": row.get("pct_change"),
                     "match_amount_yuan": row.get("match_amount_yuan")}
                    for row in amount_top50]
    behavior_candidates = [row for row in chapters["behavior_rows"]
                           if row.get("direction") in ("价升份额升(抢筹候选)", "价降份额升(放量走弱)")
                           or row.get("outrun")][:30]
    chapters["seal_quality"] = {"up_count": len(sealed_up), "down_count": len(sealed_down),
                                 "up_median_yuan": statistics.median([r["seal_yuan"] for r in sealed_up if r["seal_yuan"] is not None]) if any(r["seal_yuan"] is not None for r in sealed_up) else None,
                                 "down_median_yuan": statistics.median([r["seal_yuan"] for r in sealed_down if r["seal_yuan"] is not None]) if any(r["seal_yuan"] is not None for r in sealed_down) else None}
    if has_history:
        candidates = [(symbol, ctx["streak"]) for symbol, ctx in stock_context.items() if ctx["streak"] >= 2 and symbol in auction]
        highboards = []
        for symbol, streak in candidates:
            vals = {tag: raw_states.get(tag, {}).get(symbol, {}).get("pct_change") for tag in ("0925", "0932", "0940", "0945")}
            states = {tag: raw_states.get(tag, {}).get(symbol, {}) for tag in ("0925", "0932", "0940", "0945")}
            status = _board_status(states)
            highboards.append({"symbol": symbol, "name": stock_names.get(symbol, symbol), "streak": streak,
                               "status": status, **vals})
        chapters["yesterday_highboards"] = sorted(highboards, key=lambda row: (-row["streak"], row["symbol"]))
        a_symbols = {row["symbol"] for row in highboards} | {row["symbol"] for row in sealed_up}
        peers = []
        for code, members in relations.items():
            anchors = [s for s in members if s in a_symbols]
            if not anchors: continue
            mean = plate_means.get(code)
            for symbol in members:
                row = auction.get(symbol)
                if symbol in a_symbols or row is None or row.get("pct_change") is None: continue
                ctx = stock_context.get(symbol, {})
                pos = ctx.get("position_20d")
                relative = row["pct_change"] - mean if mean is not None else None
                peers.append({"plate": plate_names.get(code, code), "anchor_count": len(anchors), "symbol": symbol,
                              "name": stock_names.get(symbol, row.get("name") or symbol), "pct_change": row["pct_change"],
                              "match_amount_yuan": row.get("match_amount_yuan"), "position_20d": pos,
                              "position": "低位" if pos is not None and pos <= .3 else "高位" if pos is not None and pos >= .7 else "中位" if pos is not None else "位置未知",
                              "relative_to_plate_pp": relative,
                              "auction_tag": "竞价偏强" if relative is not None and relative >= 1 else "竞价偏弱" if relative is not None and relative <= -1 else ""})
        chapters["look_a_do_b"] = sorted(peers, key=lambda row: (row["plate"], -(row.get("match_amount_yuan") or 0)))[:40]
        chapters["yesterday_limit_pool"] = [{"symbol": symbol, "name": stock_names.get(symbol, row.get("name") or symbol),
                                              "pct_change": row.get("pct_change")}
                                             for symbol, row in auction.items()
                                             if stock_context.get(symbol, {}).get("streak", 0) >= 1]
    chapters["watchlist"] = {"amount_top50": amount_top50,
                             "yesterday_limit_pool": sorted(
                                 ({**row, "match_amount_yuan": auction.get(row["symbol"], {}).get("match_amount_yuan")}
                                  for row in chapters["yesterday_limit_pool"]),
                                 key=lambda row: -(row.get("match_amount_yuan") or 0))[:50],
                             "sealed_up": chapters["sealed_up"][:30],
                             "sealed_down": chapters["sealed_down"][:30],
                             "behavior_candidates": behavior_candidates}
    base = raw_states.get("0925", {})
    if stage == "open_0946" and latest:
        chapters["emerging"] = sorted([{"symbol": symbol, "name": stock_names.get(symbol, row.get("name") or symbol),
                                       "0925": base.get(symbol, {}).get("pct_change"), "0932": raw_states.get("0932", {}).get(symbol, {}).get("pct_change"),
                                       "0945": row.get("pct_change"), "match_amount_yuan": row.get("match_amount_yuan")}
                                      for symbol, row in latest.items() if base.get(symbol, {}).get("pct_change") is not None
                                      and base[symbol]["pct_change"] < 3 and row.get("pct_change") is not None and row["pct_change"] >= 5],
                                     key=lambda row: -(row.get("match_amount_yuan") or 0))[:30]
        old_plates = labels.get("0925", {}).get("top_plates_by_share", [])
        new_plates = labels.get("0945", {}).get("top_plates_by_share", [])
        old = {row["code"]: row for row in old_plates}
        changed = []
        for row in new_plates:
            before = old.get(row["code"])
            if before and before.get("equal_weight_pct") is not None and row.get("equal_weight_pct") is not None:
                changed.append({"name": row["name"], "change_pp": row["equal_weight_pct"] - before["equal_weight_pct"],
                               "current_pct": row["equal_weight_pct"], "member_count": row["member_count"]})
        chapters["plate_changes"] = {"up": sorted(changed, key=lambda row: -row["change_pp"])[:10],
                                      "down": sorted(changed, key=lambda row: row["change_pp"])[:10]}
    return chapters


def _all_plate_stats(states, relations, names):
    result = []
    for code, members in relations.items():
        selected = [states[s] for s in members if s in states and states[s].get("pct_change") is not None]
        if len(selected) < 5: continue
        result.append({"code": code, "name": names.get(code, code), "member_count": len(selected),
                       "equal_weight_pct": statistics.mean(row["pct_change"] for row in selected)})
    return result


def _board_status(states):
    present = [(tag, row) for tag, row in states.items() if row]
    if not present: return "观察"
    last_tag, last = present[-1]
    if _state_limit(last, 1): return "回封" if any(_state_limit(row, 1) for _, row in present[:-1]) and not _state_limit(present[-2][1], 1) else "封死"
    if _state_limit(last, -1): return "跌停"
    if len(present) > 1:
        previous = present[-2][1]
        if _state_limit(previous, 1): return "开板"
        if last.get("pct_change") is not None and previous.get("pct_change") is not None and last["pct_change"] < previous["pct_change"]:
            return "走弱"
    return "观察"


def _conclusion(snaps, chapters, history, stage):
    available = [snap for snap in snaps if snap.get("median_pct_change") is not None]
    if not available: return ["当前没有可计算的市场切片；请检查输入数据与追平状态。", "本页不对缺失字段作零值推断。", "高标和历史判断在数据可用前保持未知。"]
    current = available[-1]
    market_summary = chapters.get("market_price_volume", {})
    market_line = (f"市场量价依据 {current['label']}：有价个股{'累计成交额' if current.get('amount_basis') == 'cumulative_session' else '竞价总额'} {_fmt_money(current.get('market_match_amount_yuan'))}，"
                   f"对昨日终值 {_fmt_multiple(current.get('vs_previous_day_final'))}，额加权涨幅 {_fmt_pct(current.get('amount_weighted_pct_change'))}，"
                   f"下跌个股额占比 {_fmt_pct(current.get('down_amount_share_pct'))}，上涨家数占比 {_fmt_pct(current.get('up_share_pct'))}，"
                   f"开盘中位 {_fmt_pct(market_summary.get('opening_median_pct_change'))}，Top100集中度 {_fmt_pct(current.get('top100_concentration_pct'))}；"
                   f"量价状态 {current.get('market_price_volume_state')}，状态阈值待校准；数据 {current['status']}，lag {current['data_lag_seconds']} 秒。")
    comparison = current.get("auction_baseline_comparison")
    if stage == "open_0946":
        market_line += " 累计成交额不与竞价额比较。"
    elif comparison:
        market_line += (f" 64日竞价额为历史中位的 {_fmt_multiple(comparison['multiple'])}，"
                        f"历史分位 {_fmt_pct(comparison['percentile'])}；样本 {comparison['sample_days']} 日，"
                        f"范围 {_fmt_money(comparison['minimum_yi'] * 100_000_000)}–"
                        f"{_fmt_money(comparison['maximum_yi'] * 100_000_000)}。")
    else:
        market_line += " 64日竞价额基线未提供。"
    if history and history.get("median_is_historical_low"):
        market_line += f" 0925 开盘中位达到或低于近 {history['session_count']} 个历史交易日最低值。"
    elif history:
        market_line += f" 近72日开盘基线覆盖 {history['session_count']} 个交易日。"
    plates = current.get("top_plates_by_share") or []
    money_line = (f"资金分布依据：成员金额份额最高为 {plates[0]['name']}（{plates[0]['market_share_bp']:.0f}bp）。" if plates else "资金分布依据：缺少点时板块关系，当前不作板块归因。")
    sealed = chapters.get("seal_quality", {})
    risk_line = f"风险依据：封涨停 {sealed.get('up_count', 0)} 只、封跌停 {sealed.get('down_count', 0)} 只；封单状态是当前切片观测。"
    highs = chapters.get("yesterday_highboards")
    high_line = (f"高标状态依据：昨日连板高标 {len(highs)} 只，按封死/开板/回封/走弱/观察标注；后续快照可能改变状态。"
                 if isinstance(highs, list) else "高标状态依据：缺少日线历史，昨日连板身份未知。")
    return [market_line, money_line, risk_line, high_line]


def _section_body(title, payload):
    snaps = payload["snapshots"]
    chapters = payload["chapters"]
    if title == "一屏结论":
        market = chapters.get("market_price_volume", {})
        cards = [("有价个股竞价总额", _fmt_money(market.get("market_match_amount_yuan"))),
                 ("对昨日0925终值", _fmt_multiple(market.get("vs_previous_day_final"))),
                 ("对5日同刻基线", _fmt_multiple(market.get("vs_amount_baseline"))),
                 ("额加权涨幅", _fmt_pct(market.get("amount_weighted_pct_change"))),
                 ("下跌个股额占比", _fmt_pct(market.get("down_amount_share_pct"))),
                 ("上涨家数占比 / 开盘中位", f"{_fmt_pct(market.get('up_share_pct'))} / {_fmt_pct(market.get('opening_median_pct_change'))}"),
                 ("Top100额集中度", _fmt_pct(market.get("top100_concentration_pct"))),
                 ("量价状态", market.get("market_price_volume_state", "不可用"))]
        if payload["stage"] == "auction_0926":
            comparison = market.get("auction_baseline_comparison")
            cards.extend([("对64日中位", _fmt_multiple(comparison.get("multiple") if comparison else None)
                           if comparison else "基线未提供"),
                          ("对64日分位", _fmt_pct(comparison.get("percentile") if comparison else None)
                           if comparison else "基线未提供")])
        else:
            cards.append(("竞价基线口径", "累计成交额不与竞价额比较"))
        summary = "<div class='grid'>" + "".join(f"<div class='card'><small>{html.escape(k)}</small><br><b>{html.escape(str(v))}</b></div>" for k, v in cards) + "</div>"
        summary += "<p class='muted'>状态阈值待校准；Top100常态区间40–47%，偏离按百分点显示。市场量价为描述性事实，不合成总分。"
        comparison = market.get("auction_baseline_comparison")
        if payload["stage"] == "auction_0926" and comparison:
            summary += (f" 64日范围 {_fmt_money(comparison['minimum_yi'] * 100_000_000)}–"
                        f"{_fmt_money(comparison['maximum_yi'] * 100_000_000)}，样本 {comparison['sample_days']} 日。"
                        "早期基线阈值为100/500万元，略低估尾部。")
        summary += "</p>"
        return summary + "<ol>" + "".join(f"<li>{html.escape(line)}</li>" for line in payload["conclusion"]) + "</ol>"
    if title == "大盘":
        cards = []
        for snap in snaps:
            values = [snap.get("market_match_amount_yuan"), snap.get("vs_previous_day_final"),
                      snap.get("amount_weighted_pct_change"), snap.get("down_amount_share_pct"),
                      snap.get("median_pct_change"), snap.get("up_share_pct"), snap.get("top100_concentration_pct"),
                      snap.get("limit_up"), snap.get("limit_down")]
            deviation = snap.get("top100_concentration_vs_normal_pp")
            concentration_note = ("常态区间内" if snap.get("top100_concentration_in_normal_range") else
                                  f"偏离常态中点 {deviation:+.1f}pp" if deviation is not None else "不可用")
            cards.append(f"<div class='card'><b>{snap['label']} · {snap['status']}</b><br>有价个股竞价总额 {values[0]} 元<br>昨日0925终值倍数 {values[1]} · 5日基线倍数 {snap.get('vs_amount_baseline')}<br>额加权涨幅 {values[2]}%<br>下跌个股额占比 {values[3]}%<br>上涨家数占比 {values[5]}% · 开盘中位 {values[4]}%<br>Top100集中度 {values[6]}%（{concentration_note}）<br>涨停/跌停 {values[7]}/{values[8]}<br>量价状态 {snap.get('market_price_volume_state')} · 阈值待校准<br>lag {snap['data_lag_seconds']}s</div>")
        return "<div class='grid'>" + "".join(cards) + "</div>"
    if title == "板块象限":
        block = chapters["plate_quadrants"]
        counts = "；".join(f"{name} {count}" for name, count in block["counts"].items())
        intro = (f"<p>{html.escape(block['status'])}。抓取时刻：{html.escape(str(block.get('captured_local') or '—'))}。"
                 f"{html.escape(counts)}；零值边界 {block.get('zero_value_boundary_count', 0)}。"
                 "强度与净额只作描述，不暗示因果或胜负。</p>")
        if block["available"]:
            table = _table([("板块", "name"), ("象限", "quadrant"), ("强度", "strength"), ("Kaipan涨幅%", "kaipan_pct"),
                            ("我方涨幅%", "our_pct"), ("差pp", "pct_diff_pp"), ("净额元", "net_yuan"),
                            ("金额元", "amount_yuan"), ("名义成员", "member_count"),
                            ("有效价格覆盖", "effective_price_coverage"), ("提示", "flags_text")],
                           [{**row, "flags_text": "、".join(row["flags"])} for row in block["display_rows"]])
            return intro + table
        equal = _table([("我方等权涨幅前15", "name"), ("成员", "member_count"), ("涨幅%", "equal_weight_pct")], block["fallback_equal_weight"])
        down = _table([("额最大且下跌板块", "name"), ("成员", "member_count"), ("金额元", "match_amount_yuan"), ("涨幅%", "equal_weight_pct")], block["fallback_amount_down"])
        estimate = _table([("带锚点估计", "name"), ("估计强度", "estimated_strength"), ("锚点年龄秒", "anchor_age_seconds"), ("锚点时刻", "anchor_ts")], block["strength_estimates"])
        return intro + "<h3>我方等权涨幅前15（与 Kaipan 初始强度 Top20 历史重合约18/20）</h3>" + equal + "<h3>额最大且下跌的10个板块（仅价格与额推断净流出倾向）</h3>" + down + estimate
    if title == "板块联动与跌停影响":
        block = chapters["plate_linkage"]
        if not block["available"]:
            return "<p>缺少可计算板块：需要点时关系、至少8只有涨幅成员及可用切片。缺失不补0。</p>"
        rows = block["rows"]
        body = (f"<p>金额榜按 {block['slice_label']} 有额成员额排序，最多15个板块；至少8只有涨幅成员，排除泛因子。"
                "上涨占比/中位数以有涨幅成员为分母；额加权涨幅仅用同时有额和涨幅的成员。"
                "封涨/跌停额占比以板块已知成员额为分母；领涨贡献按额最大的3只有效成员计算。"
                "所有变化均为描述事实，不合成评分。</p>")
        table = _table([
            ("板块", "name"), ("成员/有价/有额/加权有效", "member_count"), ("上涨占比%", "up_share_pct"),
            ("等权中位涨幅%", "equal_weight_median_pct"), ("额加权−等权均值pp", "weighted_minus_equal_mean_pp"),
            ("封涨停数/额占比%", "limit_up_count"), ("封涨停Top3", "limit_up_top3"),
            ("封跌停数/额占比%", "limit_down_count"), ("Top3领涨贡献占比%", "top3_leading_contribution_share_pct")],
            [{**row, "member_count": (f"{row['member_count']}/{row['priced_member_count']}/"
                                       f"{row['amount_member_count']}/{row['weighted_member_count']}"),
              "limit_up_count": f"{row['limit_up_count']} / {row['limit_up_amount_share_pct']}",
              "limit_down_count": f"{row['limit_down_count']} / {row['limit_down_amount_share_pct']}",
              "limit_up_top3": ", ".join(f"{stock['name']} {stock['amount_yuan']}元" for stock in row["limit_up_top3"])}
             for row in rows])
        down = _table([("跌停集中板块", "name"), ("封跌停家数", "limit_down_count"),
                       ("跌停额占比%", "limit_down_amount_share_pct"), ("板块额元", "amount_yuan")],
                      block["down_limit_top5"])
        delta = ""
        if block.get("opening_changes") is not None:
            delta_rows = [{"name": row["name"], **row["delta"]} for row in block["opening_changes"]]
            delta = "<h3>0925→0945变化量</h3>" + _table([
                ("板块", "name"), ("上涨占比变化pp", "up_share_pct"), ("等权中位涨幅变化pp", "equal_weight_median_pct"),
                ("封涨停数变化", "limit_up_count"), ("封涨停额占比变化pp", "limit_up_amount_share_pct"),
                ("封跌停数变化", "limit_down_count"), ("封跌停额占比变化pp", "limit_down_amount_share_pct")], delta_rows)
        return body + table + "<h3>跌停集中板块 Top5</h3>" + down + delta
    if title == "板块份额":
        rows = snaps[-1]["top_plates_by_share"]
        return _table([("板块", "name"), ("成员", "member_count"), ("额", "match_amount_yuan"), ("份额bp", "market_share_bp"), ("等权涨幅%", "equal_weight_pct")], rows)
    if title == "封涨停质量":
        rows = chapters["sealed_up"]
        return (f"<p>0925 封涨停 {chapters['seal_quality']['up_count']} 只；封单额中位数 {chapters['seal_quality']['up_median_yuan']} 元。状态按后续切片观测，不推断之后结果。</p>" +
                _table([("代码", "symbol"), ("名称", "name"), ("昨连板", "streak"), ("竞价涨幅%", "pct_change"),
                        ("当前涨幅%", "current_pct"), ("当前状态", "status"), ("匹配额元", "match_amount_yuan"),
                        ("竞价未匹配买额元", "seal_yuan")], rows[:20]))
    if title == "封跌停质量":
        rows = chapters["sealed_down"]
        return (f"<p>0925 封跌停 {chapters['seal_quality']['down_count']} 只；封单额中位数 {chapters['seal_quality']['down_median_yuan']} 元。</p>" +
                _table([("代码", "symbol"), ("名称", "name"), ("竞价跌幅%", "pct_change"),
                        ("当前涨幅%", "current_pct"), ("当前状态", "status"), ("匹配额元", "match_amount_yuan"),
                        ("竞价未匹配卖额元", "seal_yuan")], rows[:20]))
    if title in ("昨日连板高标", "昨日连板高标分化"):
        rows = chapters["yesterday_highboards"]
        if rows == "缺少输入": return "<p>缺少输入：日线历史；不推断昨日连板身份。</p>"
        return _table([("昨连板", "streak"), ("代码", "symbol"), ("名称", "name"), ("竞价", "0925"),
                       ("0932", "0932"), ("0940", "0940"), ("0945", "0945"), ("状态", "status")], rows)
    if title == "昨日涨停池":
        rows = chapters["yesterday_limit_pool"]
        if chapters["yesterday_highboards"] == "缺少输入": return "<p>缺少输入：日线历史；不推断昨日涨停名单。</p>"
        return f"<p>由日线涨停幅度规则推导，当前切片覆盖 {len(rows)} 只；非 Kaipan 原始池。</p>" + _table([("代码", "symbol"), ("名称", "name"), ("竞价涨幅%", "pct_change")], rows[:30])
    if title == "竞价后新冒头":
        rows = chapters["emerging"]
        if snaps[-1].get("status") == "NOT_READY":
            return "<p>需要可用的 09:25 与当前切片；当前数据未追平或缺少竞价基准。</p>"
        emerging = _table([("代码", "symbol"), ("名称", "name"), ("0925", "0925"), ("0932", "0932"),
                           ("0945", "0945"), ("现额元", "match_amount_yuan")], rows)
        block = chapters["plate_enrichment"]
        intro = (f"<p>关联板块(富集反推, 仅关联不构成买点)：{html.escape(block['status'])}；"
                 f"成员快照 {html.escape(str(block.get('member_snapshot_date') or 'UNKNOWN'))}。</p>")
        plate_rows = []
        for plate in block.get("plates", []):
            members = "、".join(f"{row['name']}({row['symbol']}) {_fmt_pct(row['current_pct'])}"
                                for row in plate.get("members", []))
            plate_rows.append({**plate, "members_text": members})
        association = _table([("板块", "name"), ("代码", "code"), ("N", "N"), ("n", "n"),
                              ("K", "K"), ("交集", "intersection"), ("p", "p"), ("q", "q"),
                              ("交集个股", "members_text"), ("成员快照", "member_snapshot_date"),
                              ("说明", "note")], plate_rows)
        return emerging + intro + association
    if title == "竞价内变化":
        tags = chapters["behavior_tags"]
        rows = [row for row in chapters.get("behavior_rows", []) if row.get("direction") in
                ("价升份额升(抢筹候选)", "价降份额升(放量走弱)") or row.get("outrun")]
        return ("<p>价升且份额升标为抢筹候选；价降且份额升标为放量走弱。标签仅描述聚合变化，不代表交易意图；方向阈值待校准。</p>" +
                _table([("代码", "symbol"), ("名称", "name"), ("0924→0925涨幅pp", "d_pct_pp"),
                        ("份额变化bp", "d_share_bp"), ("标签", "display_tag")], rows[:20]) +
                f"<p>方向计数：{html.escape(str(tags.get('direction_counts', {})))}</p>")
    if title == "看A做B":
        rows = chapters["look_a_do_b"]
        if rows == "缺少输入": return "<p>缺少输入：日线历史；缺少点时关系时也不计算同伴。</p>"
        return _table([("板块", "plate"), ("A数量", "anchor_count"), ("同伴", "name"), ("竞价涨幅%", "pct_change"),
                       ("竞价额元", "match_amount_yuan"), ("20日位置", "position"), ("相对板块pp", "relative_to_plate_pp"),
                       ("竞价标签", "auction_tag")], rows)
    if title == "近72日对比":
        h = chapters["history_comparison"]
        if h == "缺少输入": return "<p>缺少输入：--daily-history-csv；不生成历史排名或结论。</p>"
        min_text = (f"历史最低中位 {h['history_min_median_open_pct']:.2f}%（{h['history_min_median_date']}）"
                    if h.get("history_min_median_open_pct") is not None else "历史最低中位不可用")
        return (f"<p>日线开盘涨幅=open/前一交易日收盘−1；历史样本 {h['session_count']} 个交易日。当前中位 {h['current_median_open_pct']}%，上涨占比 {h['current_up_share_pct']}%。{html.escape(min_text)}；历史最低上涨占比 {h['history_min_up_share_pct']}%（{h['history_min_up_share_date']}）。</p>" +
                _table([("日期", "date"), ("开盘涨幅中位%", "median_open_pct"), ("上涨占比%", "up_share_pct"), ("样本", "symbol_count")], h["daily"][-10:]))
    if title in ("板块回升榜", "板块回落榜"):
        key = "up" if title == "板块回升榜" else "down"
        changes = chapters["plate_changes"][key]
        if not changes: return "<p>缺少可比较板块：需点时关系与 0925、0945 切片。</p>"
        return _table([("板块", "name"), ("涨幅变化pp", "change_pp"), ("当前涨幅%", "current_pct"),
                       ("有效成员", "member_count")], changes[:10])
    return "<p>没有可用记录。</p>"


def _table(columns, rows):
    head = "".join(f"<th>{html.escape(name)}</th>" for name, _ in columns)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(row.get(key))) if row.get(key) is not None else '—'}</td>" for _, key in columns) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>" if rows else "<p>暂无可用数据。</p>"


def _fmt_pct(value): return f"{value:.2f}%" if isinstance(value, (int, float)) else "—"
def _fmt_multiple(value): return f"{value:.2f}×" if isinstance(value, (int, float)) else "—"
def _fmt_money(value): return f"{value:,.0f} 元" if isinstance(value, (int, float)) else "—"


def _json_report_payload(payload, *, full_json=False):
    import copy
    result = copy.deepcopy(payload)
    omitted = {}
    if not full_json:
        for key, container in (("snapshots", result), ("behavior_rows", result.get("chapters", {}))):
            if key not in container: continue
            value = container.pop(key)
            raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            omitted[key] = {"size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                            "row_count": len(value) if isinstance(value, list) else None}
    result.setdefault("manifest", {})["omitted_blocks"] = omitted
    return result


def write_page(out_dir, page, payload, *, full_json=False):
    path = Path(out_dir)
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise FileExistsError("out-dir must be a new or empty directory")
    path.mkdir(parents=True, exist_ok=True)
    (path / "report.html").write_text(page, encoding="utf-8")
    report = _json_report_payload(payload, full_json=full_json)
    (path / "report.json").write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--stage", choices=STAGES, required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--previous-day-final-yuan", type=int)
    parser.add_argument("--prev-final-amount", type=int, dest="previous_day_final_yuan")
    parser.add_argument("--amount-baseline-json", help="JSON scalar or object with amount_yuan for five-day same-time baseline")
    parser.add_argument("--auction-baseline-csv", help="daily auction amount baseline CSV (sum_yi in billion yuan)")
    parser.add_argument("--kaipan-hot-json", help="captured Kaipan hot-plate JSON")
    parser.add_argument("--strength-anchor-json", help="same-day plate-name/code to {strength,pct,ts} mapping")
    parser.add_argument("--daily-history-csv", help="daily_kline CSV used for streak, position and 72-session comparisons")
    parser.add_argument("--stock-names-json", help="optional symbol-to-name JSON mapping")
    parser.add_argument("--relations-dir", help="PIT registry directory containing registry.json and relation.csv")
    parser.add_argument("--full-json", action="store_true", help="include full snapshots in report.json")
    args = parser.parse_args(argv)
    usage = shutil.disk_usage(Path(args.out_dir).parent if Path(args.out_dir).parent.exists() else ".")
    if usage.used / usage.total > .90:
        parser.error("disk usage exceeds 90%; refusing to write report files")
    import os
    import taos
    td = taos.connect(host=os.getenv("TD_HOST", "127.0.0.1"), port=int(os.getenv("TD_PORT", "6030")),
                      user=os.getenv("TD_USER", "root"), password=os.getenv("TD_PASSWORD", "taosdata"),
                      database=os.getenv("TD_DATABASE", "market_data1"), config=os.getenv("TD_CONFIG", "/etc/taos"))
    try:
        relations, plate_names = {}, {}
        member_snapshot_date = "UNKNOWN"
        if args.relations_dir:
            from engine_core.auction_tracking import _load_relations
            relations, plate_names, _ = _load_relations(Path(args.relations_dir))
            member_snapshot_date = _relation_snapshot_date(args.relations_dir)
        stock_names = json.loads(Path(args.stock_names_json).read_text(encoding="utf-8")) if args.stock_names_json else None
        page, payload = render_page(td, trade_date=args.trade_date, stage=args.stage,
                                    previous_day_final_yuan=args.previous_day_final_yuan,
                                    relations=relations, plate_names=plate_names,
                                    daily_history_csv=args.daily_history_csv, stock_names=stock_names,
                                    amount_baseline_json=args.amount_baseline_json,
                                    auction_baseline_csv=args.auction_baseline_csv,
                                    kaipan_hot_json=args.kaipan_hot_json,
                                    strength_anchor_json=args.strength_anchor_json,
                                    member_snapshot_date=member_snapshot_date)
        write_page(args.out_dir, page, payload, full_json=args.full_json)
        print("TIMINGS_MS " + json.dumps(payload["manifest"]["timings_ms"], separators=(",", ":")))
    finally:
        close = getattr(td, "close", None)
        if callable(close): close()


if __name__ == "__main__":
    main()
