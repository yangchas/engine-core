"""Structured, email-safe rendering for Core daily reports."""
from __future__ import annotations

import html
import json
import math
import re
from typing import Any, Mapping

RENDER_VERSION = "core-mail-v2"
MAX_EMAIL_HTML_BYTES = 120 * 1024
_RED = "#c83b3b"
_GREEN = "#16835d"


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _num(value: Any, key: str = "") -> str:
    if value is None or isinstance(value, bool):
        return "—"
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return _esc(value)
    key_l = key.lower()
    x = float(value)
    if "yuan" in key_l or "amount" in key_l or "额" in key:
        a = abs(x)
        if a >= 100_000_000:
            return f"{x / 100_000_000:,.2f} 亿"
        if a >= 10_000:
            return f"{x / 10_000:,.2f} 万"
    if any(k in key_l for k in ("pct", "change", "median", "rate", "share")) or "涨幅" in key:
        return f"{x:+.2f}%" if abs(x) < 1000 else f"{x:,.2f}"
    return f"{x:,.2f}" if not x.is_integer() else f"{int(x):,}"


def _plain_value(value: Any, key: str = "") -> str:
    if isinstance(value, (int, float)) or value is None:
        return _num(value, key)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _conclusions(report: Mapping[str, Any], stage: str) -> list[str]:
    raw = report.get("conclusion")
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, list):
        points = [str(x).strip() for x in raw if x is not None and str(x).strip()]
        if points:
            return points[:5]
    market = report.get("market") if isinstance(report.get("market"), dict) else {}
    quality = report.get("data_quality") if isinstance(report.get("data_quality"), dict) else {}
    score = report.get("emerging_scorecard") if isinstance(report.get("emerging_scorecard"), dict) else {}
    points: list[str] = []
    if stage == "close" or "market" in report:
        if market.get("close_median") is not None:
            points.append(f"收盘市场统计（market.close_median）：{_num(market.get('close_median'), 'close_median')}。")
        if market.get("up_count") is not None or market.get("down_count") is not None:
            points.append(f"收盘市场统计（market.up_count / down_count）：上涨 {_num(market.get('up_count'))} 家、下跌 {_num(market.get('down_count'))} 家。")
        emerging = score.get("emerging")
        if isinstance(emerging, list) and emerging:
            names = [str(x.get("name", "—")) for x in emerging[:3] if isinstance(x, dict)]
            if names:
                points.append("新冒头 scorecard（emerging_scorecard.emerging）：" + "、".join(names) + "。")
        source = quality.get("daily_source") or market.get("daily_source")
        points.append("数据来源：" + (str(source) if source else "数据缺口（来源未提供）") + "。")
    if not points:
        points = ["报告已生成；结构化结论字段缺失，数据缺口：结论不可用。"]
    return points[:5]


def _metrics(report: Mapping[str, Any], stage: str) -> list[tuple[str, Any, str]]:
    if stage == "close" or "market" in report:
        market = report.get("market") if isinstance(report.get("market"), dict) else {}
        quality = report.get("data_quality") if isinstance(report.get("data_quality"), dict) else {}
        entries = [("全天成交额", market.get("amount_yuan"), "amount_yuan"),
                   ("对前日倍数", market.get("amount_vs_prev"), "ratio"),
                   ("收盘中位涨幅", market.get("close_median"), "close_median"),
                   ("上涨家数", market.get("up_count"), "count"),
                   ("涨停 / 跌停", f"{_plain_value(market.get('close_limit_up_count'))} / {_plain_value(market.get('close_limit_down_count'))}", "")]
        return entries
    terminal = report.get("auction_terminal_0925") if isinstance(report.get("auction_terminal_0925"), dict) else {}
    if not terminal and isinstance(report.get("handoff"), dict):
        terminal = report["handoff"].get("auction_0925", {})
    history = report.get("history") if isinstance(report.get("history"), dict) else {}
    return [("竞价 / 累计成交额", terminal.get("market_match_amount_yuan"), "amount_yuan"),
            ("对前日 / 基线倍数", terminal.get("vs_amount_baseline", terminal.get("vs_previous_day_final")), "ratio"),
            ("额加权涨幅", terminal.get("amount_weighted_pct_change"), "pct"),
            ("开盘中位涨幅", terminal.get("opening_median_pct_change", history.get("current_median_open_pct")), "pct"),
            ("上涨家数占比", terminal.get("up_share_pct", history.get("current_up_share_pct")), "pct"),
            ("封涨停 / 跌停", f"{_plain_value(terminal.get('sealed_up_count'))} / {_plain_value(terminal.get('sealed_down_count'))}", "")]


def _tone(key: str, value: Any) -> tuple[str, str]:
    name = key.lower()
    negative = any(word in name for word in ("down", "跌", "loss", "negative"))
    positive = any(word in name for word in ("up", "涨", "gain", "positive"))
    if isinstance(value, (int, float)):
        if value < 0:
            return _GREEN, "▼"
        if value > 0 and ("pct" in name or "change" in name or "涨幅" in key):
            return _RED, "▲"
    if positive:
        return _RED, "▲"
    if negative:
        return _GREEN, "▼"
    return "#344054", ""


def _label(key: str) -> str:
    return re.sub(r"[_]+", " ", str(key)).replace("pct", "涨幅").replace("amount", "金额")


def _chapters(report: Mapping[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    chapters = report.get("chapters") if isinstance(report.get("chapters"), dict) else {}
    out: list[tuple[str, list[dict[str, Any]]]] = []
    for key, value in chapters.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            out.append((_label(key), value[:12]))
    if "market" in report:
        for key in ("big_meat", "big_loss"):
            val = report.get(key)
            if isinstance(val, list) and val and isinstance(val[0], dict):
                out.append(("大幅走强" if key == "big_meat" else "大幅走弱", val[:10]))
        plates = report.get("plates")
        if isinstance(plates, dict):
            for key in ("close_top10", "close_bottom10", "auction_amount_top10"):
                val = plates.get(key)
                if isinstance(val, list) and val:
                    out.append((_label(key), val[:10]))
    return out[:12]


def render_email_html(report_json: Mapping[str, Any] | str | bytes, stage: str,
                      trade_date: str, manifest: Mapping[str, Any] | None = None) -> tuple[str, str]:
    """Render report JSON into compact HTML and a readable plain-text alternative."""
    if isinstance(report_json, (str, bytes)):
        report = json.loads(report_json)
    else:
        report = report_json
    if not isinstance(report, Mapping):
        raise ValueError("report JSON must be an object")
    stage_name = {"auction": "竞价", "auction_0926": "竞价", "open": "开盘", "open_0946": "开盘", "close": "收盘"}.get(stage, stage)
    manifest = manifest or (report.get("manifest") if isinstance(report.get("manifest"), dict) else {})
    asof = report.get("asof") or manifest.get("asof") or report.get("auction_terminal_0925", {}).get("asof") or "时点未提供"
    conclusions = _conclusions(report, stage)
    metrics = _metrics(report, stage)
    gaps = report.get("limitations") or report.get("data_gaps") or report.get("degraded")
    if not gaps and isinstance(manifest, Mapping):
        gaps = manifest.get("degraded") or manifest.get("limitations")
    if not gaps:
        gaps = "数据缺口：报告未提供降级摘要。"
    if isinstance(gaps, list):
        gap_text = "；".join(map(str, gaps[:8])) or "无已报告缺口"
    else:
        gap_text = str(gaps)

    title = f"Core {stage_name}报告"
    terminal = report.get("auction_terminal_0925") if isinstance(report.get("auction_terminal_0925"), dict) else {}
    source_status = terminal.get("status") or (report.get("method", {}).get("source") if isinstance(report.get("method"), dict) else None) or "来源状态未提供"
    metric_width = 100 / max(1, len(metrics))
    point_html = "".join(f'<tr><td style="padding:7px 0;color:#26364a;font-size:14px;line-height:1.55">• {_esc(p)}</td></tr>' for p in conclusions)
    metric_html = "".join(f'<td style="width:{metric_width:.2f}%;padding:10px 8px;background:#f1f5f9;border-radius:8px;vertical-align:top"><div style="font-size:11px;color:#667085">{_esc(label)}</div><div style="font-size:17px;font-weight:700;color:{_tone(label,value)[0]};padding-top:5px">{_tone(label,value)[1]} {_num(value,key) if not isinstance(value,str) else _esc(value)}</div></td>' for label,value,key in metrics)
    sections = []
    plain_sections = []
    for title_text, rows in _chapters(report):
        keys = []
        for row in rows:
            for key in row:
                if key not in keys and key not in {"symbol", "name"}:
                    keys.append(key)
        cols = [k for k in ("symbol", "name", *keys) if any(k in r for r in rows)][:6]
        header = "".join(f'<th style="text-align:left;padding:8px 7px;background:#e9eef5;color:#475467;font-size:11px">{_esc(_label(k))}</th>' for k in cols)
        body = []
        for idx,row in enumerate(rows):
            cells=[]
            for k in cols:
                value=row.get(k)
                tone, arrow = _tone(k, value)
                shown = _num(value,k) if isinstance(value,(int,float)) or value is None else _esc(value)
                if arrow: shown = f'<span style="color:{tone};font-weight:700">{arrow} {shown}</span>'
                cells.append(f'<td style="padding:7px;border-bottom:1px solid #e8edf2;background:{"#ffffff" if idx%2==0 else "#f7f9fb"};font-size:12px;color:{tone}">{shown}</td>')
            body.append("<tr>"+"".join(cells)+"</tr>")
        bar = ""
        share_key = next((k for k in ("amount_share_pct", "amount_share", "amount_share_bp", "share_pct") if rows and k in rows[0]), None)
        if share_key:
            for row in rows[:5]:
                val=row.get(share_key)
                if isinstance(val,(int,float)) and val is not None:
                    width=max(0,min(100,float(val) / (100 if share_key.endswith("_bp") else 1 if share_key.endswith("_pct") else .01 if float(val) <= 1 else 1)))
                    bar += f'<tr><td style="padding:3px 6px;font-size:11px">{_esc(row.get("name",row.get("symbol","—")))}</td><td style="width:72%;padding:3px"><table role="presentation" width="100%"><tr><td style="height:7px;background:#f0d8d8;width:{width:.1f}%;font-size:0">&nbsp;</td><td style="height:7px;background:#edf0f3;font-size:0">&nbsp;</td></tr></table></td><td style="font-size:11px;padding:3px">{_num(val,share_key)}</td></tr>'
        sections.append(f'<h2 style="font-size:16px;color:#23364d;margin:22px 0 8px">{_esc(title_text)}</h2><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse"><thead><tr>{header}</tr></thead><tbody>{"".join(body)}</tbody></table>{("<table role=\"presentation\" width=\"100%\" style=\"margin-top:8px\">"+bar+"</table>") if bar else ""}')
        plain_sections.append(title_text+"\n"+"\n".join(" | ".join(f"{_label(k)}: {_plain_value(r.get(k),k)}" for k in cols) for r in rows))

    source_data = manifest if isinstance(manifest, Mapping) else {}
    sources = source_data.get("sources", {}) if isinstance(source_data.get("sources", {}), Mapping) else {}
    rel = sources.get("kaipan_relation", {}) if isinstance(sources.get("kaipan_relation", {}), Mapping) else {}
    daily = sources.get("daily_kline", {}) if isinstance(sources.get("daily_kline", {}), Mapping) else {}
    footer = f"来源摘要：关系时点 {rel.get('asof', '未提供')} / 滞后 {rel.get('lag_trading_days', 'UNKNOWN')} 交易日；日线覆盖 {daily.get('coverage', '—')}；版本 {RENDER_VERSION}。"
    plain = f"{title} | {trade_date} | {stage_name}\n数据时点：{asof}\n\n结论\n" + "\n".join(f"• {p}" for p in conclusions)
    plain += "\n\n关键指标\n" + "\n".join(f"{label}: {_num(value,key) if not isinstance(value,str) else value}" for label,value,key in metrics)
    plain += "\n\n数据缺口与降级\n"+gap_text+"\n\n"+"\n\n".join(plain_sections)+"\n\n"+footer+"\n仅事实描述，不构成买卖建议。"
    content = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark"><title>{_esc(title)}</title><style>@media (prefers-color-scheme: dark) {{ .page{{background:#111827!important;color:#e5e7eb!important}} .card{{background:#1f2937!important;color:#e5e7eb!important}} }}</style></head><body style="margin:0;padding:16px;background:#eef2f6;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#26364a"><table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center"><table class="page" role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:680px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden"><tr><td style="background:#172b43;padding:22px 24px;color:#fff"><div style="font-size:21px;font-weight:700">{_esc(title)}</div><div style="font-size:13px;color:#d7e0eb;padding-top:7px">{_esc(trade_date)}　<span style="background:#38516e;padding:3px 8px;border-radius:10px">{_esc(stage_name)}</span>　数据时点 {_esc(asof)}　来源状态 {_esc(source_status)}</div></td></tr><tr><td style="padding:18px 20px"><table class="card" role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f6f8fb;border-left:4px solid #d49a35;padding:14px 16px"><tr><td><div style="font-size:16px;font-weight:700;padding-bottom:8px">一屏结论</div><table role="presentation" width="100%">{point_html}</table></td></tr></table><table role="presentation" width="100%" cellspacing="8" cellpadding="0" style="margin-top:14px"><tr>{metric_html}</tr></table><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:12px;background:#fff4d6;border:1px solid #f0d486;border-radius:8px"><tr><td style="padding:12px 14px;color:#765717;font-size:13px"><strong>数据缺口与降级</strong><br>{_esc(gap_text)}</td></tr></table>{''.join(sections)}<div style="border-top:1px solid #e5eaf0;margin-top:22px;padding:13px 0 2px;color:#667085;font-size:11px;line-height:1.6">口径：数值缺失显示“—”，不补零。{_esc(footer)}<br>仅事实描述，不构成买卖建议。</div></td></tr></table></td></tr></table></body></html>'''
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_EMAIL_HTML_BYTES:
        content = content.replace("".join(sections), "<p style=\"color:#667085\">明细较多，请查看附件中的完整报告。</p>")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_EMAIL_HTML_BYTES:
            raise ValueError("rendered email exceeds 120 KB")
    return content, plain
