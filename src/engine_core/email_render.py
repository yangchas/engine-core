"""Structured, email-safe rendering for Core daily reports."""
from __future__ import annotations

import html
import json
import math
import re
from typing import Any, Mapping

RENDER_VERSION = "core-mail-v3"
MAX_EMAIL_HTML_BYTES = 120 * 1024
_RED = "#c83b3b"
_GREEN = "#16835d"

# User-visible fields are allow-listed. Unknown machine fields are omitted.
FIELD_LABELS = {
    "symbol": "代码", "name": "名称", "stock_name": "名称", "main_plate": "所属板块",
    "plate": "板块", "members": "成员数", "streak": "连板数", "lb_days": "连板数",
    "status": "状态", "close_pct": "收盘涨幅", "pct": "涨跌幅", "pct_change": "涨跌幅",
    "auction_pct": "竞价涨幅", "p0925_pct": "竞价涨幅", "p0932_pct": "09:32涨幅",
    "p0945_pct": "09:45涨幅", "open_pct": "开盘涨幅", "delta_pp": "较竞价变化",
    "delta_over_limit": "收盘变化/涨跌停幅度", "auction_amount_yuan": "竞价额",
    "amount_yuan": "成交额", "match_amount_yuan": "竞价额", "match_yuan": "竞价额",
    "amount": "成交额", "amount_share": "成交额占比", "amount_share_pct": "成交额占比",
    "amount_share_bp": "成交额占比", "auction_amount_share": "竞价额占比",
    "auction_amount_rank": "竞价额排名", "rank": "排名", "score": "得分",
    "close_mean": "收盘平均涨幅", "close_median": "收盘中位涨幅",
    "auction_mean": "竞价平均涨幅", "auction_median": "竞价中位涨幅",
    "close_ge_5": "收盘涨幅≥5%", "sealed_up": "涨停价数量", "turned_negative": "收盘转负数量",
    "close_ge_5_rate": "收盘涨幅≥5%占比", "sealed_up_rate": "涨停价占比",
    "turned_negative_rate": "收盘转负占比", "n": "样本数", "matched": "匹配数",
    "close_limit_up_count": "收盘涨停家数", "close_limit_down_count": "收盘跌停家数",
    "up_count": "上涨家数", "down_count": "下跌家数", "flat_count": "平盘家数",
    "limit_pct": "涨跌停幅度", "auction_price_status": "竞价价格类型",
    "in_yest_pool": "昨日涨停池", "prior_limit_streak": "此前连板数",
    "prior_down_streak": "此前连跌数", "visibility": "出现阶段",
    "rest_bid_yuan": "未匹配买额", "rest_ask_yuan": "未匹配卖额",
    "close_at_limit_up": "收盘触及涨停价", "is_big_meat": "大肉", "is_big_loss": "大面",
    "rel_to_strongest_plate": "相对最强板块涨幅", "move_0924_0925_pp": "09:24至09:25变化",
    "auction_amount_top10": "竞价额前十", "close_top10": "收盘强势板块前十",
    "close_bottom10": "收盘弱势板块前十", "yesterday_highboards": "昨日高标",
    "sealed_up": "涨停", "sealed_down": "跌停", "emerging": "新冒头",
    "plate_changes": "板块变化", "look_a_do_b": "看A做B", "watchlist": "观察名单",
    "yesterday_limit_pool": "昨日涨停池", "market_price_volume": "市场量价",
    "plate_linkage": "板块联动", "plate_enrichment": "板块补充", "behavior_tags": "走势状态",
    "seal_quality": "封单状态", "plate_quadrants": "板块涨幅与资金", "history_comparison": "历史对照",
    "yesterday_highboards_summary": "昨日高标概况", "amount_weighted_pct_change": "成交额加权涨幅",
    "opening_median_pct_change": "开盘中位涨幅", "up_share_pct": "上涨家数占比",
    "sealed_up_count": "涨停家数", "sealed_down_count": "跌停家数",
    "market_match_amount_yuan": "竞价成交额", "vs_previous_day_final": "对前日成交额倍数",
    "vs_amount_baseline": "对历史基线倍数", "top100_concentration_pct": "成交额前百集中度",
    "data_lag_seconds": "数据滞后", "priced_symbol_count": "有价标的数",
    "symbol_count": "标的数", "auction_baseline_comparison": "竞价额历史对照",
    "amount_baseline_yuan": "竞价额基线", "source": "数据来源", "phase": "阶段",
    "cohort": "对照组", "k": "样本K", "denominator": "分母", "rate": "占比",
    "ge5_limit_negative": "收盘≥5% / 涨停价 / 转负", "meat_visibility": "大肉已达阈值",
    "loss_visibility": "大面已达阈值", "retention": "前十保持", "auction_top10": "竞价额前十",
    "close_strength_top10": "收盘强势前十", "close_weakness_top10": "收盘弱势前十",
    "prior_streak": "昨日连板", "sample_0945_pct": "09:45涨幅", "close_result_pct": "收盘涨幅",
    "description": "缺口", "0925": "竞价涨幅", "0932": "09:32涨幅", "0935": "09:35涨幅",
    "0940": "09:40涨幅", "0945": "09:45涨幅", "pct_change": "涨跌幅",
    "match_amount_yuan": "竞价额", "seal_yuan": "封单额", "current_pct": "当前涨幅",
    "amount_rank": "竞价额排名", "amount_share_bp": "竞价额占比", "auction_bp": "竞价涨幅(bp)",
}


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _humanize_copy(value: Any) -> str:
    text = str(value)
    for source, target in (
        ("wencai", "问财"),
        ("NOT_READY/STALE", "未就绪/数据滞后"),
        ("NOT_READY", "尚未就绪"), ("STALE", "数据滞后"), ("FRESH", "数据新鲜"),
        ("PIT", "点时"), ("A/B位置", "看A做B位置"), ("Top100集中度", "前100成交额集中度"),
    ):
        text = re.sub(re.escape(source), target, text, flags=re.IGNORECASE)
    text = re.sub(r"\blag\s+(\d+(?:\.\d+)?)\s*秒", r"数据滞后 \1 秒", text, flags=re.IGNORECASE)
    def amount_to_yi(match: re.Match[str]) -> str:
        amount = float(match.group(1).replace(",", ""))
        return f"{amount / 100_000_000:,.2f} 亿"
    return re.sub(r"(?<![\w.])([\d,]+(?:\.\d+)?)\s*元", amount_to_yi, text)


def _num(value: Any, key: str = "") -> str:
    if value is None or isinstance(value, bool):
        return "—"
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return _esc(value)
    key_l = key.lower()
    x = float(value)
    if "yuan" in key_l or "amount" in key_l or "额" in key:
        if abs(x) >= 100_000_000:
            return f"{x / 100_000_000:,.2f} 亿"
        if abs(x) >= 10_000:
            return f"{x / 10_000:,.2f} 万"
    if key_l.endswith("_bp") or "bp" in key_l:
        return f"{x:+.1f} bp"
    if key_l == "delta_over_limit":
        return f"{x:+.2f} 倍涨跌停幅度"
    if key_l.endswith("_pp") or "_delta" in key_l:
        return f"{x:+.2f} pp"
    if any(k in key_l for k in ("pct", "change", "median", "rate", "share", "mean")) or "涨幅" in key:
        return f"{x:+.2f}%" if abs(x) < 1000 else f"{x:,.2f}"
    if key_l.startswith("vs_") or "倍数" in key:
        return f"{x:.2f}×"
    return f"{x:,.2f}" if not x.is_integer() else f"{int(x):,}"


def _plain_value(value: Any, key: str = "") -> str:
    if isinstance(value, (int, float)) or value is None:
        return _num(value, key)
    return _esc(value)


def _label(key: str) -> str | None:
    return FIELD_LABELS.get(str(key))


def _tone(key: str, value: Any) -> tuple[str, str]:
    name = key.lower()
    is_change = any(token in name for token in ("pct", "change", "delta", "涨幅", "pp", "bp"))
    if isinstance(value, (int, float)) and is_change:
        if value < 0:
            return _GREEN, "▼"
        if value > 0:
            return _RED, "▲"
    return "#344054", ""


def _safe_points(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    points = []
    for item in raw:
        if item is None:
            continue
        point = _humanize_copy(str(item).strip())
        # Machine paths never leak into copy. Omit a point containing a
        # dotted field path, then translate known underscore field names.
        if re.search(r"\b[a-zA-Z][\w]*(?:\.[a-zA-Z_][\w]*)+\b", point):
            continue
        for key, label in sorted(FIELD_LABELS.items(), key=lambda pair: -len(pair[0])):
            if "_" in key and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]+", key):
                point = re.sub(rf"\b{re.escape(key)}\b", label, point)
        point = re.sub(r"\bFRESH\b", "数据新鲜", point, flags=re.IGNORECASE)
        point = re.sub(r"\bSTALE\b", "数据滞后", point, flags=re.IGNORECASE)
        point = re.sub(r"\bNOT_READY\b", "尚未就绪", point, flags=re.IGNORECASE)
        point = point.replace("Top100集中度", "前100成交额集中度")
        point = re.sub(r"\blag\s+(\d+(?:\.\d+)?)\s*秒", r"数据滞后 \1 秒", point, flags=re.IGNORECASE)
        def amount_to_yi(match: re.Match[str]) -> str:
            amount = float(match.group(1).replace(",", ""))
            return f"{amount / 100_000_000:,.2f} 亿"
        point = re.sub(r"(?<![\w.])([\d,]+(?:\.\d+)?)\s*元", amount_to_yi, point)
        if re.search(r"\b[a-z]+_[a-z_]+\b", point):
            continue
        if point:
            points.append(point)
    return points[:5]


def _conclusions(report: Mapping[str, Any], stage: str) -> list[str]:
    points = _safe_points(report.get("conclusion"))
    if points:
        return points
    market = report.get("market") if isinstance(report.get("market"), dict) else {}
    if stage == "close" or "market" in report:
        if market.get("close_median") is not None:
            points.append(f"收盘中位涨幅为 {_num(market['close_median'], 'close_median')}。")
        if market.get("up_count") is not None or market.get("down_count") is not None:
            points.append(f"上涨 {_num(market.get('up_count'))} 家、下跌 {_num(market.get('down_count'))} 家。")
    return points


def _metrics(report: Mapping[str, Any], stage: str) -> list[tuple[str, Any, str]]:
    if stage == "close" or "market" in report:
        market = report.get("market") if isinstance(report.get("market"), dict) else {}
        candidates = [("全天成交额", market.get("amount_yuan"), "amount_yuan"),
                      ("对前日倍数", market.get("amount_vs_prev"), "vs_amount"),
                      ("收盘中位涨幅", market.get("close_median"), "close_median"),
                      ("上涨家数", market.get("up_count"), "count"),
                      ("下跌家数", market.get("down_count"), "count"),
                      ("涨停 / 跌停", f"{_plain_value(market.get('close_limit_up_count'))} / {_plain_value(market.get('close_limit_down_count'))}", "")]
    else:
        terminal = report.get("auction_terminal_0925") if isinstance(report.get("auction_terminal_0925"), dict) else {}
        if not terminal and isinstance(report.get("handoff"), dict):
            terminal = report["handoff"].get("auction_0925", {})
        history = report.get("history") if isinstance(report.get("history"), dict) else {}
        candidates = [("竞价 / 累计成交额", terminal.get("market_match_amount_yuan"), "amount_yuan"),
                     ("对前日 / 基线倍数", terminal.get("vs_amount_baseline", terminal.get("vs_previous_day_final")), "vs_amount"),
                     ("成交额加权涨幅", terminal.get("amount_weighted_pct_change"), "pct"),
                     ("开盘中位涨幅", terminal.get("opening_median_pct_change", history.get("current_median_open_pct")), "pct"),
                     ("上涨家数占比", terminal.get("up_share_pct", history.get("current_up_share_pct")), "pct"),
                     ("涨停 / 跌停", f"{_plain_value(terminal.get('sealed_up_count'))} / {_plain_value(terminal.get('sealed_down_count'))}", "")]
    return [(label, value, key) for label, value, key in candidates if value is not None]


def _chapters(report: Mapping[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    chapters = report.get("chapters") if isinstance(report.get("chapters"), dict) else {}
    out = []
    for key, value in chapters.items():
        label = _label(key)
        if label and isinstance(value, list) and value and isinstance(value[0], dict):
            out.append((label, value[:12]))
    if "market" in report:
        for key in ("big_meat", "big_loss"):
            val = report.get(key)
            if isinstance(val, list) and val and isinstance(val[0], dict):
                out.append(("大肉" if key == "big_meat" else "大面", val[:10]))
        plates = report.get("plates")
        if isinstance(plates, dict):
            for key in ("close_top10", "close_bottom10", "auction_amount_top10"):
                val = plates.get(key)
                if isinstance(val, list) and val:
                    out.append((FIELD_LABELS[key], val[:10]))
    terminal = report.get("auction_terminal_0925") if isinstance(report.get("auction_terminal_0925"), Mapping) else {}
    top_stocks = terminal.get("top_stocks_by_amount") if isinstance(terminal, Mapping) else None
    if isinstance(top_stocks, list) and top_stocks:
        total = terminal.get("market_match_amount_yuan")
        stock_rows = []
        for source_row in top_stocks[:10]:
            row = dict(source_row)
            if isinstance(row.get("pct_change"), (int, float)):
                row["auction_bp"] = round(float(row["pct_change"]) * 100, 1)
            if isinstance(total, (int, float)) and total > 0 and isinstance(row.get("match_amount_yuan"), (int, float)):
                row["amount_share_pct"] = row["match_amount_yuan"] / total * 100
            stock_rows.append(row)
        out.append(("竞价额前列个股", stock_rows))
    top_plates = terminal.get("top_plates_by_share") if isinstance(terminal, Mapping) else None
    if isinstance(top_plates, list) and top_plates:
        out.append(("板块竞价额占比", top_plates[:10]))
    editorial = report.get("email_editorial")
    if isinstance(editorial, dict):
        for title, rows in editorial.items():
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                out.append(("收盘核心对照" if title == "SCORECARD" else title, rows[:20]))
    return out[:16]


def _metadata(manifest: Mapping[str, Any], report: Mapping[str, Any], stage: str) -> list[str]:
    lines = []
    asof = report.get("asof") or manifest.get("asof")
    if asof:
        lines.append(f"数据时点：{asof}")
    if stage == "close":
        daily = report.get("daily_manifest") if isinstance(report.get("daily_manifest"), Mapping) else {}
        source = daily.get("source") or (report.get("data_quality") or {}).get("daily_source")
        if source:
            item = f"日线来源：{_esc(_humanize_copy(source))}"
            if daily.get("symbol_count") is not None:
                item += f"，覆盖 {_num(daily['symbol_count'])} 只"
            if daily.get("fetched_at"):
                item += f"，抓取时间 { _esc(daily['fetched_at'])}"
            lines.append(item)
    else:
        sources = manifest.get("sources", {}) if isinstance(manifest.get("sources"), Mapping) else {}
        td = sources.get("td", {}) if isinstance(sources.get("td"), Mapping) else {}
        terminal = report.get("auction_terminal_0925", {})
        if not terminal and isinstance(report.get("handoff"), Mapping):
            terminal = report["handoff"].get("auction_0925", {})
        lag = td.get("data_lag_seconds", terminal.get("data_lag_seconds"))
        coverage = td.get("coverage") if isinstance(td.get("coverage"), Mapping) else {}
        if lag is not None:
            lines.append(f"数据滞后：{_num(lag)} 秒")
        if coverage.get("symbols") is not None:
            lines.append(f"覆盖：{_num(coverage.get('priced_symbols'))} / {_num(coverage['symbols'])} 只")
    return lines


def render_email_html(report_json: Mapping[str, Any] | str | bytes, stage: str,
                      trade_date: str, manifest: Mapping[str, Any] | None = None) -> tuple[str, str]:
    """Render report JSON into compact HTML and readable plain text."""
    report = json.loads(report_json) if isinstance(report_json, (str, bytes)) else report_json
    if not isinstance(report, Mapping):
        raise ValueError("report JSON must be an object")
    stage_key = str(report.get("stage") or stage).lower()
    stage_name = "收盘" if "close" in stage_key else "开盘" if "open" in stage_key else "竞价"
    manifest = manifest or (report.get("manifest") if isinstance(report.get("manifest"), Mapping) else {})
    meta = _metadata(manifest, report, stage_key)
    conclusions = _conclusions(report, stage_key)
    daily_stage = any(token in stage_key for token in ("close", "auction", "open"))
    if daily_stage or report.get("sample_days") == 1 or (isinstance(report.get("email_editorial"), dict) and report["email_editorial"].get("单日描述")):
        if not any("单日描述" in p for p in conclusions):
            conclusions.append("单日描述，仅呈现当日检查点和结算结果。")
    if not conclusions:
        conclusions = ["报告已生成；当前没有可展示的结论字段。"]
    conclusions = conclusions[:5]
    metrics = _metrics(report, stage_key)
    editorial_gaps = report.get("email_editorial", {}).get("数据缺口列表") if isinstance(report.get("email_editorial"), Mapping) else None
    if editorial_gaps:
        gaps = [str(row.get("description")) for row in editorial_gaps if isinstance(row, Mapping) and row.get("description")]
    else:
        gaps = report.get("limitations") or report.get("data_gaps") or report.get("degraded") or manifest.get("degraded") or manifest.get("missing")
    if isinstance(gaps, list):
        gaps = [str(x) for x in gaps if x]
    gap_text = "；".join(_humanize_copy(gap) for gap in gaps[:8]) if isinstance(gaps, list) else _humanize_copy(gaps) if gaps else ""
    title = f"Core {stage_name}报告"
    status = None
    terminal = report.get("auction_terminal_0925") if isinstance(report.get("auction_terminal_0925"), Mapping) else {}
    if isinstance(report.get("handoff"), Mapping) and not terminal:
        terminal = report["handoff"].get("auction_0925", {})
    status = terminal.get("status") or (report.get("method", {}).get("source") if isinstance(report.get("method"), Mapping) else None)
    status_labels = {
        "FINAL": "终价", "INDICATIVE": "指示价", "READY": "可用",
        "FRESH": "数据新鲜", "STALE": "数据滞后", "NOT_READY": "尚未就绪",
        "TD stock_tick_v2 (read-only)": "逐笔行情（只读）",
    }
    if status is not None:
        status = status_labels.get(str(status), status)
    meta_html = "　".join(_esc(x) for x in meta + ([f"来源状态：{status}"] if status else []))
    points_html = "".join(f'<tr><td style="padding:7px 0;font-size:14px;line-height:1.55">• {_esc(p)}</td></tr>' for p in conclusions)
    width = 100 / max(1, len(metrics))
    metric_html = "".join(f'<td style="width:{width:.2f}%;padding:10px 8px;background:#f1f5f9;border-radius:8px;vertical-align:top"><div style="font-size:11px;color:#667085">{_esc(label)}</div><div style="font-size:16px;font-weight:700;padding-top:5px">{_num(value,key) if not isinstance(value,str) else _esc(value)}</div></td>' for label,value,key in metrics)
    sections, plain_sections = [], []
    for chapter_title, rows in _chapters(report):
        keys = []
        for row in rows:
            for key in row:
                if key not in keys and _label(key):
                    keys.append(key)
        cols = keys[:6]
        if not cols:
            continue
        header = "".join(f'<th style="text-align:left;padding:8px 7px;background:#e9eef5;color:#475467;font-size:11px">{_esc(_label(k))}</th>' for k in cols)
        body = []
        for idx, row in enumerate(rows):
            cells = []
            for key in cols:
                value = row.get(key)
                tone, arrow = _tone(key, value)
                if key == "status" and value is not None:
                    shown = _esc({"FINAL": "终价", "INDICATIVE": "指示价", "READY": "可用", "FRESH": "数据新鲜", "STALE": "数据滞后", "NOT_READY": "尚未就绪"}.get(str(value), value))
                else:
                    shown = _num(value, key) if isinstance(value, (int, float)) or value is None else _esc(value)
                if arrow:
                    shown = f'<span style="color:{tone};font-weight:700">{arrow} {shown}</span>'
                cells.append(f'<td style="padding:7px;border-bottom:1px solid #e8edf2;background:{"#ffffff" if idx%2==0 else "#f7f9fb"};font-size:12px;color:{tone}">{shown}</td>')
            body.append("<tr>" + "".join(cells) + "</tr>")
        share_key = next((k for k in ("amount_share_pct", "amount_share", "amount_share_bp", "share_pct") if any(k in row for row in rows)), None)
        bars = []
        if share_key:
            for row in rows[:5]:
                val = row.get(share_key)
                if isinstance(val, (int, float)):
                    divisor = 100 if share_key.endswith("_bp") else 1
                    bar_width = max(0, min(100, float(val) / divisor))
                    bars.append(f'<tr><td style="padding:3px 6px;font-size:11px">{_esc(row.get("name", row.get("symbol", "—")))}</td><td style="width:72%;padding:3px"><table role="presentation" width="100%"><tr><td style="height:7px;background:#c83b3b;width:{bar_width:.1f}%;font-size:0">&nbsp;</td><td style="height:7px;background:#edf0f3;font-size:0">&nbsp;</td></tr></table></td><td style="font-size:11px;padding:3px">{_num(val,share_key)}</td></tr>')
        bar_html = f'<table role="presentation" width="100%" style="margin-top:8px">{"".join(bars)}</table>' if bars else ""
        sections.append(f'<h2 style="font-size:16px;color:#23364d;margin:22px 0 8px">{_esc(chapter_title)}</h2><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse"><thead><tr>{header}</tr></thead><tbody>{"".join(body)}</tbody></table>{bar_html}')
        plain_sections.append(chapter_title + "\n" + "\n".join(" | ".join(f"{_label(k)}：{_plain_value(r.get(k),k)}" for k in cols) for r in rows))
    close_extra = ""
    if isinstance(report.get("email_editorial"), Mapping):
        paragraphs = report["email_editorial"].get("结论", [])
        if paragraphs:
            close_extra = '<h2 style="font-size:16px;margin-top:22px">收盘复盘</h2><ul>' + "".join(f"<li>{_esc(p)}</li>" for p in paragraphs) + "</ul>"
            plain_sections.append("收盘复盘\n" + "\n".join(map(str, paragraphs)))
    gap_html = f'<section style="margin-top:14px;background:#fff4d6;padding:12px 14px"><strong>数据缺口与降级</strong><br>{_esc(gap_text)}</section>' if gap_text else ""
    footer = "口径：缺失显示“—”，不补零。仅事实描述，不构成买卖建议。"
    plain = f"{title} | {trade_date}\n" + "\n".join(meta) + "\n\n一屏结论\n" + "\n".join(f"• {p}" for p in conclusions)
    if metrics:
        plain += "\n\n关键指标\n" + "\n".join(f"{label}：{_num(value,key) if not isinstance(value,str) else value}" for label,value,key in metrics)
    if gap_text:
        plain += "\n\n数据缺口与降级\n" + gap_text
    plain += "\n\n" + "\n\n".join(plain_sections) + "\n\n" + footer
    content = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark"><title>{_esc(title)}</title><style>@media (prefers-color-scheme: dark) {{ .page{{background:#111827!important;color:#e5e7eb!important}} }}</style></head><body style="margin:0;padding:16px;background:#eef2f6;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#26364a"><table role="presentation" width="100%"><tr><td align="center"><table class="page" role="presentation" width="100%" style="max-width:680px;background:#fff;border-radius:12px"><tr><td style="background:#172b43;padding:22px 24px;color:#fff"><div style="font-size:21px;font-weight:700">{_esc(title)}</div><div style="font-size:12px;padding-top:7px">{_esc(trade_date)}　{meta_html}</div></td></tr><tr><td style="padding:18px 20px"><section style="background:#f6f8fb;padding:14px 16px"><strong>一屏结论</strong><table width="100%">{points_html}</table></section><table width="100%" cellspacing="8" style="margin-top:14px"><tr>{metric_html}</tr></table>{gap_html}{''.join(sections)}{close_extra}<div style="border-top:1px solid #e5eaf0;margin-top:22px;padding:13px 0;color:#667085;font-size:11px">{_esc(footer)}</div></td></tr></table></td></tr></table></body></html>'''
    if len(content.encode("utf-8")) > MAX_EMAIL_HTML_BYTES:
        raise ValueError("rendered email exceeds 120 KB")
    return content, plain
