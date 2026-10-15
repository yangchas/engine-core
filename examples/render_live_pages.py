#!/usr/bin/env python3
"""Render a self-contained, read-only auction or opening status page from injected TD."""
from __future__ import annotations

import argparse
import html
import json
import shutil
import statistics
from datetime import datetime
from pathlib import Path

from engine_core.live_session import (build_live_slice, checkpoint_ready, daily_history_context,
                                      load_daily_history_csv, states_at)

STAGES = {"auction_0926": [("0920", "09:20:00"), ("0924", "09:24:00"), ("0925", "09:25:06")],
          "open_0946": [("0925", "09:25:06"), ("0932", "09:32:00"), ("0935", "09:35:00"),
                        ("0940", "09:40:00"), ("0945", "09:45:30")]}
SECTIONS = {"auction_0926": ["一屏结论", "大盘", "板块份额", "封涨停质量", "封跌停质量", "昨日连板高标", "竞价内变化", "看A做B", "近72日对比"],
            "open_0946": ["一屏结论", "大盘", "昨日连板高标分化", "昨日涨停池", "竞价后新冒头", "板块回升榜", "板块回落榜", "封涨停质量", "封跌停质量", "竞价内变化", "看A做B", "近72日对比"]}


def _rows(states, top=20):
    return sorted((dict(row) for row in states.values()),
                  key=lambda row: (-(int(row.get("match_amount_yuan") or 0)), row.get("symbol", "")))[:top]


def render_page(td, *, trade_date, stage, previous_day_final_yuan=None,
                relations=None, plate_names=None, daily_history_csv=None, stock_names=None):
    if stage not in STAGES: raise ValueError(f"unknown stage: {stage}")
    snaps, raw_states = [], {}
    for label, clock in STAGES[stage]:
        cutoff = datetime.fromisoformat(f"{trade_date}T{clock}")
        if clock == "09:45:30" and not checkpoint_ready(td, cutoff):
            snaps.append({"label": label, "asof": cutoff.isoformat(timespec="seconds"),
                          "status": "NOT_READY", "data_lag_seconds": None,
                          "market_match_amount_yuan": None, "vs_previous_day_final": None,
                          "median_pct_change": None, "up_share_pct": None, "limit_up": None,
                          "limit_down": None, "top_plates_by_share": [], "top_stocks_by_amount": []})
            continue
        rows = states_at(td, cutoff)
        raw_states[label] = rows
        snap = build_live_slice(rows, cutoff, previous_day_final_yuan=previous_day_final_yuan,
                                relations=relations, plate_names=plate_names)
        snap["label"] = label
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
    conclusion = _conclusion(snaps, chapters, history, stage)
    limitations = "板块关系使用调用方提供的点时关系；名称来自可选名称表或代码。日线输入缺失时，昨日连板、A/B位置与72日比较明确降级；缺失字段不补零。09:45 切片未追平时标记 NOT_READY/STALE。"
    payload = {"trade_date": trade_date, "stage": stage, "snapshots": snaps, "sections": sections,
               "conclusion": conclusion, "history": history, "chapters": chapters,
               "limitations": limitations,
               "method": {"source": "TD stock_tick_v2 (read-only)",
                          "auction_price": "09:25:00 前用有效一档匹配价；09:25:00 起用 px_milli 和 amt_yuan 终值",
                          "amount": "竞价期为虚拟匹配额；开盘后为 TD 成交额累计值",
                          "missing": "缺字段保持 null，不补 0",
                          "limitations": limitations}}
    content = "".join(f"<section><h2>{html.escape(title)}</h2>{_section_body(title, payload)}</section>" for title in sections)
    encoded = html.escape(json.dumps(payload, ensure_ascii=False, indent=2))
    page = f'''<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{trade_date} {stage}</title><style>
:root{{color-scheme:light dark;--bg:#f4f6fa;--fg:#172033;--panel:#fff;--accent:#2563eb;--muted:#667085}}
@media(prefers-color-scheme:dark){{:root{{--bg:#111827;--fg:#e5e7eb;--panel:#1f2937;--accent:#60a5fa;--muted:#aab4c3}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,sans-serif}}main{{max-width:1100px;margin:auto;padding:20px}}h1{{font-size:1.55rem}}section{{background:var(--panel);border-radius:12px;padding:16px;margin:14px 0;overflow:auto}}h2{{font-size:1.2rem;margin:0 0 12px}}table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}td,th{{text-align:left;padding:7px;border-bottom:1px solid #8892a033}}small,.muted{{color:var(--muted)}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:9px}}.card{{padding:10px;background:var(--bg);border-radius:8px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}</style></head>
<body><main><h1>{trade_date} · {stage}</h1><p class="muted">只读观测；来源、口径与局限见页面末尾。状态值按可用数据计算。</p>{content}
<section><h2>口径与局限</h2><pre>{encoded}</pre></section></main></body></html>'''
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
        return chapters
    # C155 tags use the canonical 0920/0924/0925 values and market totals.
    market = {tag: {"m": sum(int(row.get("match_amount_yuan") or 0) for row in raw_states.get(tag, {}).values())}
              for tag in ("0920", "0924", "0925")}
    plate_pct = {}
    auction_state = raw_states.get("0925", {})
    for code, members in relations.items():
        values = [auction_state[s]["pct_change"] for s in members if s in auction_state and auction_state[s].get("pct_change") is not None]
        if len(values) >= 5: plate_pct[code] = statistics.mean(values)
    tagged = {}
    for symbol in set().union(*(set(raw_states.get(tag, {})) for tag in ("0920", "0924", "0925"))):
        stock = {"symbol": symbol, "prev_day_amount": stock_context.get(symbol, {}).get("prev_day_amount_yuan"),
                 "plates_0925": {code: pct for code, pct in plate_pct.items() if symbol in relations.get(code, ())}}
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
        means = {}
        for code, members in relations.items():
            live = [auction[s] for s in members if s in auction and auction[s].get("pct_change") is not None]
            if live: means[code] = statistics.mean(row["pct_change"] for row in live)
        peers = []
        for code, members in relations.items():
            anchors = [s for s in members if s in a_symbols]
            if not anchors: continue
            mean = means.get(code)
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
    base = raw_states.get("0925", {})
    if stage == "open_0946" and latest:
        chapters["emerging"] = sorted([{"symbol": symbol, "name": stock_names.get(symbol, row.get("name") or symbol),
                                       "0925": base.get(symbol, {}).get("pct_change"), "0932": raw_states.get("0932", {}).get(symbol, {}).get("pct_change"),
                                       "0945": row.get("pct_change"), "match_amount_yuan": row.get("match_amount_yuan")}
                                      for symbol, row in latest.items() if base.get(symbol, {}).get("pct_change") is not None
                                      and base[symbol]["pct_change"] < 3 and row.get("pct_change") is not None and row["pct_change"] >= 5],
                                     key=lambda row: -(row.get("match_amount_yuan") or 0))[:30]
        old_plates = _all_plate_stats(raw_states.get("0925", {}), relations, plate_names)
        new_plates = _all_plate_stats(raw_states.get("0945", {}), relations, plate_names)
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
    market_line = f"大盘依据 {current['label']} 中位涨幅 {current['median_pct_change']:.2f}%、上涨占比 {current['up_share_pct']:.1f}%；数据状态 {current['status']}，lag {current['data_lag_seconds']} 秒。"
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
        return "<ol>" + "".join(f"<li>{html.escape(line)}</li>" for line in payload["conclusion"]) + "</ol>"
    if title == "大盘":
        cards = []
        for snap in snaps:
            values = [snap.get("market_match_amount_yuan"), snap.get("vs_previous_day_final"),
                      snap.get("median_pct_change"), snap.get("up_share_pct"), snap.get("limit_up"), snap.get("limit_down")]
            cards.append(f"<div class='card'><b>{snap['label']} · {snap['status']}</b><br>金额 {values[0]}<br>昨日终值倍数 {values[1]}<br>中位涨幅 {values[2]}<br>上涨占比 {values[3]}%<br>涨停/跌停 {values[4]}/{values[5]}<br>lag {snap['data_lag_seconds']}s</div>")
        return "<div class='grid'>" + "".join(cards) + "</div>"
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
        return _table([("代码", "symbol"), ("名称", "name"), ("0925", "0925"), ("0932", "0932"),
                       ("0945", "0945"), ("现额元", "match_amount_yuan")], rows)
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


def write_page(out_dir, page, payload):
    path = Path(out_dir)
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise FileExistsError("out-dir must be a new or empty directory")
    path.mkdir(parents=True, exist_ok=True)
    (path / "report.html").write_text(page, encoding="utf-8")
    (path / "report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--stage", choices=STAGES, required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--previous-day-final-yuan", type=int)
    parser.add_argument("--prev-final-amount", type=int, dest="previous_day_final_yuan")
    parser.add_argument("--daily-history-csv", help="daily_kline CSV used for streak, position and 72-session comparisons")
    parser.add_argument("--stock-names-json", help="optional symbol-to-name JSON mapping")
    parser.add_argument("--relations-dir", help="PIT registry directory containing registry.json and relation.csv")
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
        if args.relations_dir:
            from engine_core.auction_tracking import _load_relations
            relations, plate_names, _ = _load_relations(Path(args.relations_dir))
        stock_names = json.loads(Path(args.stock_names_json).read_text(encoding="utf-8")) if args.stock_names_json else None
        page, payload = render_page(td, trade_date=args.trade_date, stage=args.stage,
                                    previous_day_final_yuan=args.previous_day_final_yuan,
                                    relations=relations, plate_names=plate_names,
                                    daily_history_csv=args.daily_history_csv, stock_names=stock_names)
        write_page(args.out_dir, page, payload)
    finally:
        close = getattr(td, "close", None)
        if callable(close): close()


if __name__ == "__main__":
    main()
