"""Compact, scan-first email layout for auction, open and close reports."""
from __future__ import annotations

import html
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _short(value: Any, limit: int = 16) -> str:
    text = "—" if value is None else str(value).replace("\n", " ").replace("\r", " ")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _num(value: Any, digits: int = 1) -> str:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(x):
        return "—"
    return f"{x:+.{digits}f}%" if x > 0 else f"{x:.{digits}f}%"


def _amount(value: Any) -> str:
    try:
        return f"{float(value) / 1e8:.1f}"
    except (TypeError, ValueError):
        return "—"


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [r for r in value if isinstance(r, Mapping)] if isinstance(value, list) else []


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    return next((row[k] for k in keys if row.get(k) is not None), None)


def _metric(label: str, value: str, color: str = "#f1f5f9") -> tuple[str, str]:
    return label, f'<td style="padding:7px 8px;background:{color};vertical-align:top"><div style="font-size:10px;color:#667085">{_esc(label)}</div><b style="display:block;padding-top:3px;font-size:14px">{_esc(value)}</b></td>'


def _tiles(report: Mapping[str, Any], stage: str) -> list[tuple[str, str]]:
    market = report.get("market") if isinstance(report.get("market"), Mapping) else {}
    tiles: list[tuple[str, str]] = []
    median_key = "close_median" if stage == "close" else "p0945_median" if stage == "open" else "auction_median"
    median_label = "收盘中位" if stage == "close" else "09:45中位" if stage == "open" else "竞价中位"
    if market.get(median_key) is not None:
        tiles.append(_metric(median_label, _num(market.get(median_key))))
    up, down = ((market.get("auction_up_count"), market.get("auction_down_count")) if stage == "auction" else
                (_first(market, "open_up_count", "up_count"), _first(market, "open_down_count", "down_count")))
    if up is not None or down is not None:
        tiles.append(_metric("涨/跌", f"{up if up is not None else '—'} / {down if down is not None else '—'}"))
    if stage == "close" and (market.get("close_limit_up_count") is not None or market.get("close_limit_down_count") is not None):
        tiles.append(_metric("涨停价/跌停价", f"{market.get('close_limit_up_count', '—')} / {market.get('close_limit_down_count', '—')}"))
    yesterday = market.get("yesterday_limit_pool") or report.get("yesterday_review")
    if isinstance(yesterday, Mapping):
        avg = _first(yesterday, "today_mean_pct", "close_mean", "mean_pct")
        red = _first(yesterday, "red_rate", "red_share", "up_rate")
        if avg is not None or red is not None:
            tiles.append(_metric("昨板表现", f"{_num(avg)} / 红{_num(float(red) * 100) if isinstance(red, (int, float)) and red <= 1 else _num(red)}"))
        elif yesterday.get("status"):
            tiles.append(_metric("昨板表现", "缺报告"))
    ladder = report.get("ladder_section") if isinstance(report.get("ladder_section"), Mapping) else {}
    summary = _rows(ladder.get("summary"))
    top = _first(summary[0], "max_price_streak", "max_streak", "board_height") if summary else None
    if top is None:
        top = market.get("max_price_streak")
    if top is not None:
        high_count = _first(summary[0], "max_price_streak_count", "space_count", "today_n") if summary else None
        value = _short(top, 8) + (f" / {high_count}只" if high_count is not None else "")
        tiles.append(_metric("最高板", value))
    auction_base = market.get("auction_baseline_comparison")
    if isinstance(auction_base, Mapping):
        multiple, pctile = auction_base.get("multiple"), auction_base.get("percentile")
        if multiple is not None or pctile is not None:
            tiles.append(_metric("竞价额", f"{float(multiple):.2f}× / {float(pctile):.0f}%分位" if multiple is not None and pctile is not None else f"{multiple if multiple is not None else '—'}× / {pctile if pctile is not None else '—'}%"))
    elif market.get("auction_baseline_multiple") is not None or market.get("auction_baseline_percentile") is not None:
        tiles.append(_metric("竞价额", f"{market.get('auction_baseline_multiple', '—')}× / {market.get('auction_baseline_percentile', '—')}%分位"))
    daily_multiple = _first(market, "amount_vs_prev_common", "amount_vs_prev")
    if stage == "close" and daily_multiple is not None:
        tiles.append(_metric("全天额", f"{float(daily_multiple):.2f}×前日"))
    # Keep a readable 4–6 tile strip; combine only when fields exceed the strip.
    return tiles[:6]


def _section(title: str, body: str, note: str = "") -> str:
    suffix = f'<div style="color:#667085;font-size:10px;margin-top:4px">{_esc(note)}</div>' if note else ""
    return f'<section style="margin:10px 0;padding:9px 10px;background:#fff;border:1px solid #e7ebf0"><h2 style="font-size:14px;margin:0 0 6px;color:#23364d">{_esc(title)}</h2>{body}{suffix}</section>'


def _ladder(report: Mapping[str, Any], stage: str) -> tuple[str, str]:
    data = report.get("ladder_section")
    if not isinstance(data, Mapping):
        msg = "数据缺口：未生成连板梯队"
        return _section("连板梯队", f'<div>{msg}</div>'), msg
    rows = _rows(data.get("rows"))
    if not rows:
        reason = _short(data.get("status") or "无梯队明细", 18)
        msg = f"数据缺口：连板梯队 {reason}"
        return _section("连板梯队", f'<div>{_esc(msg)}</div>'), msg
    levels: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        try:
            prior = int(_first(row, "prior_streak", "lb_days", "streak"))
        except (TypeError, ValueError):
            continue
        levels[max(1, prior + 1)].append(row)
    summary = _rows(data.get("summary"))
    summary_by_level = {str(r.get("board_height")): r for r in summary if r.get("board_height") is not None}
    yesterday_counts = {str(r.get("board_height")): r.get("count") for r in _rows(data.get("yesterday_board_counts"))}
    lines = []
    ordered = sorted(levels.items(), reverse=True)
    for board, items in ordered:
        stat = summary_by_level.get(f"{board - 1}板") or summary_by_level.get(f"{board}板") or {}
        n = _first(stat, "previous_n", "previous_limit_n", "count")
        if n is None:
            n = yesterday_counts.get(f"{board - 1}板")
        k = _first(stat, "promoted_n", "today_n")
        label = f"{board}板 昨{n if n is not None else '—'} 晋{k if k is not None else '—'}/{n if n is not None else '—'}"
        chips = []
        for row in sorted(items, key=lambda r: (-float(r.get("auction_amount_yuan") or 0), str(r.get("symbol", ""))))[:12]:
            pct = row.get("close_pct") if stage == "close" else row.get("p0945_pct") if stage == "open" else _first(row, "p0925_pct", "auction_pct")
            limit = row.get("limit_pct")
            if stage == "close" and isinstance(pct, (int, float)) and isinstance(limit, (int, float)):
                state = "封" if pct >= limit - .2 else "触" if isinstance(row.get("high_pct"), (int, float)) and row["high_pct"] >= limit - .2 else "断"
            elif stage == "open" and isinstance(pct, (int, float)) and isinstance(limit, (int, float)):
                state = "封" if pct >= limit - .2 else "触" if isinstance(row.get("high_pct"), (int, float)) and row["high_pct"] >= limit - .2 else "—"
            else:
                state = "一字" if row.get("one_word") or row.get("is_one_word") else "封" if row.get("auction_limit_state") in (1, "1", "UP") else "—"
            if row.get("is_new_listing") or row.get("unlimited_price_band"):
                state = "新"
            name = _short(row.get("name") or row.get("symbol"), 8)
            chips.append(f'<span style="display:inline-block;margin:1px 3px 1px 0;padding:3px 5px;background:#f1f5f9;white-space:nowrap">{_esc(name)} {_esc(_num(pct))} {state} {_amount(row.get("auction_amount_yuan"))}亿</span>')
        emphasis = "background:#fff1ed;padding:0 3px" if board == max(levels) else ""
        lines.append(f'<div style="line-height:23px"><b style="display:inline-block;min-width:94px;color:#9b3e32;{emphasis}">{_esc(label)}</b>{"".join(chips)}</div>')
    firsts = [r for r in rows if _first(r, "prior_streak", "lb_days", "streak") in (0, "0")]
    firsts.sort(key=lambda r: (-float(r.get("auction_amount_yuan") or 0), str(r.get("symbol", ""))))
    if firsts:
        chips = "　".join(f"{_short(r.get('name') or r.get('symbol'), 8)} {_num(r.get('close_pct') if stage == 'close' else r.get('p0945_pct') if stage == 'open' else _first(r, 'p0925_pct', 'auction_pct'))}" for r in firsts[:8])
        lines.append(f'<div style="line-height:23px"><b>首板 {len(firsts)}只</b>　{_esc(chips)}</div>')
    space = f'<div style="font-size:10px;color:#667085;margin-top:3px">空间板 {max(levels) if levels else "—"}板；价格状态为代理</div>'
    text = "连板梯队：" + "；".join(f"{b}板 昨{_first(summary_by_level.get(f'{b-1}板', {}), 'previous_n', 'previous_limit_n', 'count') or yesterday_counts.get(f'{b-1}板', '—')} 晋{_first(summary_by_level.get(f'{b-1}板', {}), 'promoted_n', 'today_n') or '—'}/{_first(summary_by_level.get(f'{b-1}板', {}), 'previous_n', 'previous_limit_n', 'count') or yesterday_counts.get(f'{b-1}板', '—')}" for b, items in ordered)
    return _section("连板梯队", "".join(lines) + space, "按昨日板数分层；封/触/断为价格代理") , text


def _extremes(report: Mapping[str, Any], stage: str) -> tuple[str, str]:
    blocks, plain = [], []
    exceptional = _rows(report.get("unlimited_price_band_stocks"))
    if stage == "close":
        groups = [("big_meat", "大肉 TOP8"), ("big_loss", "大面 TOP8")]
    else:
        key = "p0925_pct" if stage == "auction" else "p0945_pct"
        title_up = "竞价强 TOP8" if stage == "auction" else "开盘强 TOP8"
        title_down = "竞价弱 TOP8" if stage == "auction" else "开盘弱 TOP8"
        source_rows = _rows(report.get("stocks"))
        if not source_rows:
            chapters = report.get("chapters") if isinstance(report.get("chapters"), Mapping) else {}
            source_rows = _rows(chapters.get("yesterday_highboards"))
        values = [r for r in source_rows if not (r.get("is_new_listing") or r.get("is_unlimited_price_band") or r.get("unlimited_price_band"))
                  and isinstance(_first(r, key, "auction_pct" if stage == "auction" else "p0945_pct"), (int, float))]
        groups = [("__stage_up", title_up), ("__stage_down", title_down)]
        report = dict(report)
        report["__stage_up"] = sorted(values, key=lambda r: -float(_first(r, key, "auction_pct" if stage == "auction" else "p0945_pct")))
        report["__stage_down"] = sorted(values, key=lambda r: float(_first(r, key, "auction_pct" if stage == "auction" else "p0945_pct")))
    for key, title in groups:
        raw = _rows(report.get(key))
        exceptional.extend(r for r in raw if r.get("is_new_listing") or r.get("is_unlimited_price_band") or r.get("unlimited_price_band"))
        source = [r for r in raw if not (r.get("is_new_listing") or r.get("is_unlimited_price_band") or r.get("unlimited_price_band"))][:8]
        if not source:
            msg = f"数据缺口：{title} 未生成"
            blocks.append(_section(title, f'<div>{_esc(msg)}</div>'))
            plain.append(msg)
            continue
        headers = ["名称", "板块", "竞价%"]
        if stage != "auction":
            headers.append("09:45")
        headers.extend(["额", "状态"])
        if stage == "close":
            headers.insert(4, "收盘%")
        body = []
        for index, row in enumerate(source):
            name = _short(row.get("name") or row.get("symbol"), 10)
            code = _esc(str(row.get("symbol") or "")[-6:])
            plate = _short(row.get("main_plate") or "—", 4)
            auction = _first(row, "auction_pct", "p0925_pct")
            p945 = row.get("p0945_pct") if stage != "auction" else None
            amount = _amount(_first(row, "auction_amount_yuan", "match_amount_yuan"))
            state = "新" if row.get("is_new_listing") or row.get("is_unlimited_price_band") else "—"
            if stage == "close" and row.get("close_at_limit_up"):
                state = "封"
            elif stage == "open" and isinstance(p945, (int, float)) and isinstance(row.get("limit_pct"), (int, float)) and p945 >= row["limit_pct"] - .2:
                state = "封"
            cells = [f'{_esc(name)}<small style="display:block;color:#667085">{code}</small>', _esc(plate), _esc(_num(auction))]
            if stage != "auction":
                cells.append(_esc(_num(p945)))
            if stage == "close":
                cells.append(_esc(_num(row.get("close_pct"))))
            cells.extend([f"{amount}亿", state])
            bg = "#fff" if index % 2 == 0 else "#f7f9fb"
            body.append("<tr>" + "".join(f'<td style="padding:4px 5px;background:{bg};white-space:nowrap;border-bottom:1px solid #e8edf2">{c}</td>' for c in cells) + "</tr>")
        th = "".join(f'<th style="text-align:left;padding:4px 5px;background:#e9eef5;white-space:nowrap">{_esc(h)}</th>' for h in headers)
        table = f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-size:11px"><thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table>'
        blocks.append(_section(title, table))
        plain.append(f"{title}：" + "、".join(f"{_short(r.get('name') or r.get('symbol'), 8)} {_num(r.get('close_pct') if stage == 'close' else r.get('p0945_pct') if stage == 'open' else _first(r, 'auction_pct', 'p0925_pct'))}" for r in source))
    exceptional = list({str(r.get("symbol") or r.get("name")): r for r in exceptional}.values())
    if exceptional:
        names = "、".join(_short(r.get("name") or r.get("symbol"), 8) for r in exceptional[:8])
        note = f"新股/无涨跌幅限制：{names}"
        blocks.append(_section("价格带例外", f"<div>{_esc(note)}</div>"))
        plain.append(note)
    return "".join(blocks), "\n".join(plain)


def _spark(values: Sequence[Any]) -> str:
    levels = "▁▂▃▄▅▆▇█"
    nums = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(float(v))]
    if not nums:
        return "—"
    lo, hi = min(nums), max(nums)
    if hi == lo:
        return "▄" * min(4, len(nums))
    return "".join(levels[round((x - lo) / (hi - lo) * 7)] for x in nums[:4])


def _plates(report: Mapping[str, Any], stage: str) -> tuple[str, str]:
    data = report.get("plates") if isinstance(report.get("plates"), Mapping) else {}
    top = _rows(data.get("auction_amount_top10"))[:8]
    weak = _rows(data.get("close_bottom10"))[:5] if stage == "close" else []
    chosen = [(r, "主线") for r in top] + [(r, "弱势") for r in weak]
    if not chosen:
        msg = "数据缺口：板块事实未生成"
        return _section("板块盘面", f'<div>{msg}</div>'), msg
    headers = ["类", "板块", "人数", "竞价额", "占比bp", "竞价%"]
    if stage != "auction":
        headers.append("09:45")
    if stage == "close":
        headers.append("收盘%")
    headers.append("趋势")
    table_rows, notes = [], []
    for index, (row, kind) in enumerate(chosen):
        amount = _first(row, "auction_amount_yuan", "match_amount_yuan")
        share = _first(row, "auction_amount_share_bp", "amount_share_bp", "auction_amount_bp")
        if share is None:
            ratio = _first(row, "auction_amount_share", "amount_share")
            share = float(ratio) * 10000 if isinstance(ratio, (int, float)) else None
        pct = _first(row, "p0925_mean", "auction_mean", "mean_pct", "pct")
        if stage == "close" and isinstance(row.get("trend"), list):
            values = row["trend"]
        elif stage == "auction":
            values = [row.get("p0920_mean"), row.get("p0924_mean"), pct]
        else:
            values = [pct, row.get("p0932_mean"), row.get("p0945_mean"), row.get("close_mean")]
        values = values[:3] if stage == "auction" else values[:3] if stage == "open" else values
        cells = [kind, _short(row.get("name") or row.get("plate") or "—", 8), str(_first(row, "members", "count") or "—"), _amount(amount), f"{float(share):.0f}" if isinstance(share, (int, float)) else "—", _num(pct)]
        if stage != "auction":
            cells.append(_num(row.get("p0945_mean")))
        if stage == "close":
            cells.append(_num(row.get("close_mean")))
        cells.append(_spark(values))
        bg = "#fff" if index % 2 == 0 else "#f7f9fb"
        table_rows.append("<tr>" + "".join(f'<td style="padding:4px 5px;background:{bg};white-space:nowrap;border-bottom:1px solid #e8edf2">{_esc(c)}</td>' for c in cells) + "</tr>")
        if kind == "主线" and isinstance(share, (int, float)):
            notes.append(f"{_short(row.get('name'), 8)} {share:.0f}bp")
    th = "".join(f'<th style="text-align:left;padding:4px 5px;background:#e9eef5;white-space:nowrap">{_esc(h)}</th>' for h in headers)
    table = f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-size:11px"><thead><tr>{th}</tr></thead><tbody>{"".join(table_rows)}</tbody></table>'
    note = ("额单位：亿元；份额单位：bp且成员重叠不可加总；趋势：竞价→09:32→09:45→收盘。"
            if stage == "close" else "额单位：亿元；份额单位：bp且成员重叠不可加总。")
    return _section("板块盘面", table, note), "板块盘面：" + "；".join(notes)


def _a_to_b(report: Mapping[str, Any], stage: str) -> tuple[str, str]:
    entries = _rows(report.get("a_to_b"))
    if not isinstance(report.get("a_to_b"), list):
        msg = "数据缺口：未生成看A做B"
        return _section("看A做B", f'<div>{msg}</div>'), msg
    if not entries:
        msg = "看A做B：无合格A"
        return _section("看A做B", f'<div>{msg}</div>'), msg
    output, plain = [], []
    for entry in entries[:8]:
        anchor = f"A {_short(entry.get('a_name') or entry.get('name') or entry.get('a_symbol'), 8)} {_short(entry.get('prior_streak') or entry.get('streak') or '—', 6)}板"
        anchor += f" {_num(_first(entry, 'auction_pct', 'p0925_pct'))}"
        if stage == "close":
            anchor += f"→{_num(entry.get('close_pct'))}"
        candidates = _rows(entry.get("candidates"))[:3]
        chips = []
        for row in candidates:
            text = f"B {_short(row.get('name') or row.get('symbol'), 8)} {_num(_first(row, 'auction_pct', 'p0925_pct'))} {_amount(row.get('auction_amount_yuan'))}亿"
            if stage != "auction":
                text += f" {_num(row.get('p0945_pct'))}"
            if stage == "close":
                text += f" {_num(row.get('close_pct'))}"
            chips.append(text)
        reason = "、".join(chips) if chips else _short(entry.get("reason") or "无候选", 10)
        content = f'<div style="line-height:23px"><b>{_esc(anchor)}</b><div style="padding-left:8px">{_esc(reason)}</div></div>'
        output.append(content)
        plain.append(anchor + " → " + reason)
    return _section("看A做B", "".join(output), "点时同板块候选；不构成建议"), "看A做B：" + "；".join(plain)


def _other(report: Mapping[str, Any]) -> tuple[str, str]:
    gaps = []
    for key in ("data_gaps", "limitations", "degraded"):
        value = report.get(key)
        if isinstance(value, list):
            gaps.extend(_short(x, 24) for x in value if x)
        elif value:
            gaps.append(_short(value, 24))
    quality = report.get("data_quality") if isinstance(report.get("data_quality"), Mapping) else {}
    if quality.get("daily_source"):
        gaps.append("日线:" + _short(quality.get("daily_source"), 10))
    market = report.get("market") if isinstance(report.get("market"), Mapping) else {}
    if not isinstance(market.get("auction_baseline_comparison"), Mapping) and market.get("auction_baseline_multiple") is None:
        gaps.append("竞价额历史基线未生成")
    yesterday = report.get("yesterday_review")
    if not isinstance(yesterday, Mapping) or yesterday.get("status") == "UNAVAILABLE_NO_PREV_REPORT":
        gaps.append("昨板回看缺前日报告")
    unique = list(dict.fromkeys(gaps))[:3]
    msg = "；".join(unique) if unique else "缺失不补零；仅描述当日事实"
    return _section("数据与口径", f'<div>{_esc(msg)}</div>'), msg


def render_email_board(report: Mapping[str, Any], stage: str, trade_date: str) -> tuple[str, str]:
    """Return a compact complete email HTML document and a matching text view."""
    stage_key = "close" if any(x in stage.lower() for x in ("close", "closing", "eod")) else "open" if "open" in stage.lower() else "auction"
    stage_label = {"close": "收盘", "open": "开盘", "auction": "竞价"}[stage_key]
    tiles = _tiles(report, stage_key)
    tile_html = ("<table role=\"presentation\" width=\"100%\" cellspacing=\"3\" cellpadding=\"0\"><tr>" + "".join(x[1] for x in tiles) + "</tr></table>") if tiles else "<div>数据缺口：情绪温度计字段不足</div>"
    ladder_html, ladder_text = _ladder(report, stage_key)
    extreme_html, extreme_text = _extremes(report, stage_key)
    plate_html, plate_text = _plates(report, stage_key)
    ab_html, ab_text = _a_to_b(report, stage_key)
    other_html, other_text = _other(report)
    body = (f'<div style="max-width:680px;margin:0 auto;background:#f5f7fa;color:#263445;font-family:Arial,"Microsoft YaHei",sans-serif;font-size:12px;line-height:1.45;font-variant-numeric:tabular-nums">'
            f'<header style="padding:10px 2px"><b>{_esc(trade_date)} {stage_label}盘面</b></header>'
            f'{_section("情绪温度计", tile_html)}{ladder_html}{extreme_html}{plate_html}{ab_html}{other_html}'
            '<footer style="padding:8px;color:#667085;font-size:10px">缺失不补零；涨停状态为价格代理；单日事实不构成预测或建议。</footer></div>')
    if "close" != stage_key:
        # Defense in depth for both markup and plain output.
        for forbidden in ("close_pct", "close_mean", "close_median", "收盘%", "收盘中位", "收盘"):
            assert forbidden not in body
    document = '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="margin:0;background:#f5f7fa">' + body + "</body></html>"
    plain = f"{trade_date} {stage_label}盘面\n情绪温度计：" + "；".join(f"{label} {html.unescape(re.sub(r'<[^>]*>', '', val))}" for label, val in tiles) + f"\n{ladder_text}\n{extreme_text}\n{plate_text}\n{ab_text}\n数据与口径：{other_text}"
    return document, plain
