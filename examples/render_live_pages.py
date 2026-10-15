#!/usr/bin/env python3
"""Render a self-contained, read-only auction or opening status page from injected TD."""
from __future__ import annotations

import argparse
import html
import json
import shutil
from datetime import datetime
from pathlib import Path

from engine_core.live_session import build_live_slice, checkpoint_ready, states_at

STAGES = {"auction_0926": [("0920", "09:20:00"), ("0924", "09:24:00"), ("0925", "09:25:06")],
          "open_0946": [("0925", "09:25:06"), ("0932", "09:32:00"), ("0935", "09:35:00"),
                        ("0940", "09:40:00"), ("0945", "09:45:30")]}
SECTIONS = {"auction_0926": ["大盘", "板块份额", "封涨停质量", "昨日高标", "竞价内变化", "看A做B", "近72日"],
            "open_0946": ["大盘", "昨日高标分化", "竞价后新冒头", "板块回升榜", "板块回落榜"]}


def _rows(states, top=20):
    return sorted((dict(row) for row in states.values()),
                  key=lambda row: (-(int(row.get("match_amount_yuan") or 0)), row.get("symbol", "")))[:top]


def render_page(td, *, trade_date, stage, previous_day_final_yuan=None,
                relations=None, plate_names=None):
    if stage not in STAGES: raise ValueError(f"unknown stage: {stage}")
    snaps = []
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
        snap = build_live_slice(rows, cutoff, previous_day_final_yuan=previous_day_final_yuan,
                                relations=relations, plate_names=plate_names)
        snap["label"] = label
        snap["top_stocks_by_amount"] = _rows(rows)
        snaps.append(snap)
    sections = SECTIONS[stage]
    limitations = "板块关系使用调用方提供的点时关系；缺少关系或前日终值时对应比较留空。72 日历史、昨日连板池等未提供时不推断。"
    payload = {"trade_date": trade_date, "stage": stage, "snapshots": snaps, "sections": sections,
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


def _section_body(title, payload):
    snaps = payload["snapshots"]
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
        rows = [row for row in snaps[-1]["top_stocks_by_amount"] if row.get("price_milli") and row.get("prev_close_milli")]
        from engine_core.auction_tracking import _at_limit
        rows = [row for row in rows if _at_limit(row, "up") or _at_limit(row, "down")]
        return _table([("代码", "symbol"), ("涨幅%", "pct_change"), ("金额", "match_amount_yuan"),
                       ("封买股数", "rest_bid_shares"), ("封卖股数", "rest_ask_shares")], rows)
    if title in ("昨日高标", "昨日高标分化"):
        return "<p>未提供昨日连板池及历史连板数，不生成高标名单或分化结论。</p>"
    if title == "竞价后新冒头":
        auction = next((snap for snap in snaps if snap["label"] == "0925"), None)
        if auction is None or snaps[-1].get("status") == "NOT_READY":
            return "<p>需要可用的 09:25 与当前切片；当前数据未追平或缺少竞价基准。</p>"
        base = {row.get("symbol"): row for row in auction["top_stocks_by_amount"]}
        rows = [row for row in snaps[-1]["top_stocks_by_amount"]
                if row.get("symbol") in base and base[row["symbol"]].get("pct_change") is not None
                and base[row["symbol"]]["pct_change"] < 3 and row.get("pct_change") is not None
                and row["pct_change"] >= 5]
        return _table([("代码", "symbol"), ("现涨幅%", "pct_change"), ("现额", "match_amount_yuan")], rows)
    if title in ("竞价内变化", "看A做B"):
        return "<p>需要历史切片、昨日涨停池及点时关系输入；本次未提供的字段不作推断。</p>"
    if title == "近72日": return "<p>未注入历史基线；不生成历史排名或结论。</p>"
    if title in ("板块回升榜", "板块回落榜"):
        first, last = snaps[0], snaps[-1]
        if not first.get("top_plates_by_share") or not last.get("top_plates_by_share"):
            return "<p>缺少点时板块关系或 09:25 基准，不计算板块变化。</p>"
        before = {row["code"]: row for row in first["top_plates_by_share"]}
        changes = []
        for row in last["top_plates_by_share"]:
            old = before.get(row["code"])
            if old and old.get("equal_weight_pct") is not None and row.get("equal_weight_pct") is not None:
                changes.append({"name": row["name"], "change": row["equal_weight_pct"] - old["equal_weight_pct"],
                                "equal_weight_pct": row["equal_weight_pct"], "market_share_bp": row["market_share_bp"]})
        changes.sort(key=lambda row: row["change"], reverse=title == "板块回升榜")
        return _table([("板块", "name"), ("涨幅变化pp", "change"), ("当前涨幅%", "equal_weight_pct"),
                       ("当前份额bp", "market_share_bp")], changes[:10])
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
        page, payload = render_page(td, trade_date=args.trade_date, stage=args.stage,
                                    previous_day_final_yuan=args.previous_day_final_yuan,
                                    relations=relations, plate_names=plate_names)
        write_page(args.out_dir, page, payload)
    finally:
        close = getattr(td, "close", None)
        if callable(close): close()


if __name__ == "__main__":
    main()
