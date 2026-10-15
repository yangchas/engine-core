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
    stocks = _rows(report.get("stocks"))
    if stocks and any("in_yest_pool" in row for row in stocks):
        pool = [row for row in stocks if row.get("in_yest_pool") is True or row.get("in_yest_pool") in (1, "1")]
        closes = [float(row["close_pct"]) for row in pool if isinstance(row.get("close_pct"), (int, float))]
        if closes:
            red = sum(value > 0 for value in closes) / len(closes)
            red_count = sum(value > 0 for value in closes)
            tiles.append(_metric("昨板表现", f"{_num(sum(closes) / len(closes))} / 红{red_count}/{len(closes)} ({red * 100:.1f}%)"))
        else:
            tiles.append(_metric("昨板表现", "数据缺口：昨日池无收盘值"))
    else:
        yesterday = market.get("yesterday_limit_pool") or report.get("yesterday_review")
        if isinstance(yesterday, Mapping):
            avg = _first(yesterday, "today_mean_pct", "close_mean", "mean_pct")
            red = _first(yesterday, "red_rate", "red_share", "up_rate")
            if avg is not None or red is not None:
                tiles.append(_metric("昨板表现", f"{_num(avg)} / 红{_num(float(red) * 100) if isinstance(red, (int, float)) and red <= 1 else _num(red)}"))
            elif yesterday.get("status"):
                tiles.append(_metric("昨板表现", "数据缺口：缺昨板统计"))
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
    stock_by_symbol = {str(row.get("symbol", "")).zfill(6): row for row in _rows(report.get("stocks"))}
    for row in rows:
        merged = dict(stock_by_symbol.get(str(row.get("symbol", "")).zfill(6), {}))
        merged.update(row)
        row = merged
        try:
            prior = int(_first(row, "prior_streak", "lb_days", "streak"))
        except (TypeError, ValueError):
            continue
        levels[max(1, prior + 1)].append(row)
    summary = _rows(data.get("summary"))
    summary_by_level = {str(r.get("board_height")): r for r in summary if r.get("board_height") is not None}
    yesterday_counts = {str(r.get("board_height")): r.get("count") for r in _rows(data.get("yesterday_board_counts"))}
    promotions = data.get("promotion_by_previous_level") if isinstance(data.get("promotion_by_previous_level"), Mapping) else {}
    if not promotions and data.get("promotion_status") != "MISSING_YESTERDAY_POOL":
        stocks = _rows(report.get("stocks"))
        if any("in_yest_pool" in row for row in stocks):
            pool = [row for row in stocks if row.get("in_yest_pool") is True or row.get("in_yest_pool") in (1, "1")]
            for row in pool:
                try:
                    prior = int(row.get("lb_days"))
                except (TypeError, ValueError):
                    continue
                item = promotions.setdefault(f"{prior}板", {"promoted": 0, "total": 0})
                item["total"] += 1
                band, close = row.get("limit_pct"), row.get("close_pct")
                if isinstance(band, (int, float)) and isinstance(close, (int, float)) and band > 0 and close >= band - .2:
                    item["promoted"] += 1
    lines = []
    plain_lines = []
    ordered = sorted(levels.items(), reverse=True)
    for board, items in ordered:
        stat = summary_by_level.get(f"{board - 1}板") or summary_by_level.get(f"{board}板") or {}
        n = _first(stat, "previous_n", "previous_limit_n", "count")
        if n is None:
            n = yesterday_counts.get(f"{board - 1}板")
        promotion = promotions.get(f"{board - 1}板")
        k = promotion.get("promoted") if isinstance(promotion, Mapping) else _first(stat, "promoted_n", "today_n")
        if isinstance(promotion, Mapping):
            n = promotion.get("total")
        if n is None:
            n = yesterday_counts.get(f"{board - 1}板")
        if k is None and n is not None:
            k = _first(stat, "promoted_n", "today_n")
        label = f"{board}板 昨{n if n is not None else '—'} 晋{k if k is not None else '—'}/{n if n is not None else '—'}"
        chips = []
        plain_chips = []
        for row in sorted(items, key=lambda r: (-float(r.get("auction_amount_yuan") or 0), str(r.get("symbol", ""))))[:12]:
            pct = row.get("close_pct") if stage == "close" else row.get("p0945_pct") if stage == "open" else _first(row, "p0925_pct", "auction_pct")
            limit = row.get("limit_pct")
            is_new = row.get("is_new_listing") or row.get("is_unlimited_price_band")
            if is_new:
                state = "新"
            elif stage == "close" and isinstance(pct, (int, float)) and isinstance(limit, (int, float)):
                state = "封" if pct >= limit - .2 else "触" if isinstance(row.get("high_pct"), (int, float)) and row["high_pct"] >= limit - .2 else "跌" if pct <= -limit + .2 else ""
            elif stage == "open" and isinstance(pct, (int, float)) and isinstance(limit, (int, float)):
                state = "封" if pct >= limit - .2 else "触" if isinstance(row.get("high_pct"), (int, float)) and row["high_pct"] >= limit - .2 else ""
            else:
                state = "封" if row.get("auction_limit_state") in (1, "1", "UP") else ""
            if row.get("is_new_listing") or row.get("unlimited_price_band"):
                state = "新"
            name = _short(row.get("name") or row.get("symbol"), 8)
            chips.append(f'<span style="display:inline-block;margin:1px 3px 1px 0;padding:3px 5px;background:#f1f5f9;white-space:nowrap">{_esc(name)} {_esc(_num(pct))} {state} {_amount(row.get("auction_amount_yuan"))}亿</span>')
            plain_chips.append(f"{name} {_num(pct)} {state} {_amount(row.get('auction_amount_yuan'))}亿".strip())
        emphasis = "background:#fff1ed;padding:0 3px" if board == max(levels) else ""
        lines.append(f'<div style="line-height:23px"><b style="display:inline-block;min-width:94px;color:#9b3e32;{emphasis}">{_esc(label)}</b>{"".join(chips)}</div>')
        plain_lines.append(label + (" " + "、".join(plain_chips) if plain_chips else ""))
    firsts = [r for r in rows if _first(r, "prior_streak", "lb_days", "streak") in (0, "0")]
    firsts.sort(key=lambda r: (-float(r.get("auction_amount_yuan") or 0), str(r.get("symbol", ""))))
    if firsts:
        chips = "　".join(f"{_short(r.get('name') or r.get('symbol'), 8)} {_num(r.get('close_pct') if stage == 'close' else r.get('p0945_pct') if stage == 'open' else _first(r, 'p0925_pct', 'auction_pct'))}" for r in firsts[:8])
        lines.append(f'<div style="line-height:23px"><b>首板 {len(firsts)}只</b>　{_esc(chips)}</div>')
    gap_note = "" if data.get("promotion_status") != "MISSING_YESTERDAY_POOL" else "；数据缺口：缺昨日涨停池，晋级率不可算"
    space = f'<div style="font-size:10px;color:#667085;margin-top:3px">空间板 {max(levels) if levels else "—"}板；封/触为价格代理，非封单{gap_note}</div>'
    text = "连板梯队：\n" + "\n".join(plain_lines)
    return _section("连板梯队", "".join(lines) + space, "按昨日板数分层；封/触为价格代理，非封单") , text


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
        headers.append("额")
        if stage == "close":
            headers.insert(4, "收盘%")
        body = []
        states = []
        for index, row in enumerate(source):
            name = _short(row.get("name") or row.get("symbol"), 10)
            code = _esc(str(row.get("symbol") or "")[-6:])
            plate = _short(row.get("main_plate") or "—", 4)
            auction = _first(row, "auction_pct", "p0925_pct")
            p945 = row.get("p0945_pct") if stage != "auction" else None
            amount = _amount(_first(row, "auction_amount_yuan", "match_amount_yuan"))
            limit = row.get("limit_pct")
            close = row.get("close_pct")
            state = "新" if row.get("is_new_listing") or row.get("is_unlimited_price_band") else ""
            if stage == "close" and isinstance(limit, (int, float)) and isinstance(close, (int, float)):
                state = "封" if close >= limit - .2 else "触" if isinstance(row.get("high_pct"), (int, float)) and row["high_pct"] >= limit - .2 else "跌" if close <= -limit + .2 else state
            elif stage == "close" and row.get("close_at_limit_up"):
                state = "封"
            elif stage == "open" and isinstance(p945, (int, float)) and isinstance(limit, (int, float)) and p945 >= limit - .2:
                state = "封"
            states.append(state)
            cells = [f'{_esc(name)}<small style="display:block;color:#667085">{code}</small>', _esc(plate), _esc(_num(auction))]
            if stage != "auction":
                cells.append(_esc(_num(p945)))
            if stage == "close":
                cells.append(_esc(_num(row.get("close_pct"))))
            cells.append(f"{amount}亿")
            bg = "#fff" if index % 2 == 0 else "#f7f9fb"
            body.append("<tr>" + "".join(f'<td style="padding:4px 5px;background:{bg};white-space:nowrap;border-bottom:1px solid #e8edf2">{c}</td>' for c in cells) + "</tr>")
        if any(states):
            headers.append("状态")
            # Append the proxy state after the amount cell.
            for row_index, state in enumerate(states):
                marker = f'<td style="padding:4px 5px;background:{"#fff" if row_index % 2 == 0 else "#f7f9fb"};white-space:nowrap;border-bottom:1px solid #e8edf2">{_esc(state)}</td>'
                body[row_index] = body[row_index].replace("</tr>", marker + "</tr>")
        th = "".join(f'<th style="text-align:left;padding:4px 5px;background:#e9eef5;white-space:nowrap">{_esc(h)}</th>' for h in headers)
        table = f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-size:11px"><thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table>'
        blocks.append(_section(title, table, "状态仅标价格代理" if any(states) else "状态列省略；状态仅标价格代理"))
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
    narrow = _rows(data.get("narrow_theme_top5"))[:5]
    if not narrow:
        stocks = _rows(report.get("stocks"))
        by_name: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        metadata = {str(row.get("name")): row for key in ("auction_amount_top10", "close_top10", "close_bottom10", "deferred_generic_event")
                    for row in _rows(data.get(key)) if row.get("name")}
        for stock in stocks:
            for name in stock.get("plates", []) if isinstance(stock.get("plates"), list) else []:
                by_name[str(name)].append(stock)
        market_total = sum(float(r.get("auction_amount_yuan") or 0) for r in stocks)
        from .plate_kind import classify_plate_kind
        for name, members in by_name.items():
            meta = metadata.get(name, {})
            kind = str(meta.get("kind") or classify_plate_kind(name)).upper()
            if not 8 <= len(members) <= 150 or kind not in {"THEME", "NICHE"}:
                continue
            amount = sum(float(r.get("auction_amount_yuan") or 0) for r in members)
            pct_keys = [r.get("p0925_pct") for r in members if isinstance(r.get("p0925_pct"), (int, float))]
            close_keys = [r.get("close_pct") for r in members if isinstance(r.get("close_pct"), (int, float))]
            narrow.append({"name": name, "kind": kind, "members": len(members),
                           "auction_amount_yuan": amount,
                           "auction_amount_share": amount / market_total if market_total else None,
                           "p0925_mean": sum(pct_keys) / len(pct_keys) if pct_keys else None,
                           "p0945_mean": None, "close_mean": sum(close_keys) / len(close_keys) if close_keys else None})
        narrow.sort(key=lambda r: (-(r.get("auction_amount_share") or 0), r["name"]))
        narrow = narrow[:5]
    chosen = [(r, "主线") for r in top] + [(r, "窄主题") for r in narrow] + [(r, "弱势") for r in weak]
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
        entry = dict(entry)
        anchor_stock = next((row for row in _rows(report.get("stocks"))
                             if str(row.get("symbol", "")).zfill(6) == str(entry.get("a_symbol", "")).zfill(6)), {})
        if entry.get("a_auction_pct") is None:
            entry["a_auction_pct"] = _first(anchor_stock, "p0925_pct", "auction_pct")
        if entry.get("a_close_pct") is None:
            entry["a_close_pct"] = anchor_stock.get("close_pct")
        anchor_name = _short(entry.get('a_name') or entry.get('name') or entry.get('a_symbol'), 8)
        anchor_parts = [f"A {anchor_name}"]
        streak = _first(entry, "a_prior_streak", "prior_streak", "streak")
        if streak is not None:
            anchor_parts.append(f"昨{streak}板")
        auction = _first(entry, "a_auction_pct", "auction_pct", "p0925_pct")
        close = _first(entry, "a_close_pct", "close_pct")
        anchor_parts.append(_num(auction) if auction is not None else "竞价缺失")
        if stage == "close":
            anchor_parts.append("→" + (_num(close) if close is not None else "收盘缺失"))
        anchor = " ".join(anchor_parts)
        candidates = _rows(entry.get("candidates"))[:3]
        chips = []
        for row in candidates:
            text = f"B {_short(row.get('name') or row.get('symbol'), 8)} {_num(_first(row, 'auction_pct', 'p0925_pct'))} {_amount(row.get('auction_amount_yuan'))}亿 {_position_label(row)}"
            if stage != "auction":
                text += f" {_num(row.get('p0945_pct'))}"
            if stage == "close":
                text += f" {_num(row.get('close_pct'))}"
            chips.append(text)
        reason_map = {"无合格主题板块": "无合格板块", "无合格同伴": "无同伴",
                      "同伴全部买不到": "同伴买不到", "缺少竞价价/额": "缺竞价数据"}
        reason = "、".join(chips) if chips else reason_map.get(str(entry.get("reason") or ""), _short(entry.get("reason") or "无同伴", 10))
        content = f'<div style="line-height:23px"><b>{_esc(anchor)}</b><div style="padding-left:8px">{_esc(reason)}</div></div>'
        output.append(content)
        plain.append(anchor + " → " + reason)
    return _section("看A做B", "".join(output), "点时同板块候选；不构成建议"), "看A做B：\n" + "\n".join(plain)


def _position_label(row: Mapping[str, Any]) -> str:
    raw = row.get("position")
    if isinstance(raw, str):
        if "低于前20日区间" in raw:
            return "越界↓"
        if "高于前20日区间" in raw:
            return "越界↑"
        if raw in {"低", "中", "高", "越界↑", "越界↓"}:
            return raw
    value = _first(row, "prior_range_position20_pct", "p0925_range_position20_pct")
    if value is None and isinstance(raw, str):
        try:
            value = float(raw.rstrip("%"))
        except ValueError:
            value = None
    if isinstance(value, (int, float)):
        if value < 0:
            return "越界↓"
        if value > 100:
            return "越界↑"
        return "低" if value <= 33 else "中" if value <= 67 else "高"
    return "位置未知"


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
    has_pool = any("in_yest_pool" in row for row in _rows(report.get("stocks")))
    if not has_pool and (not isinstance(yesterday, Mapping) or yesterday.get("status") == "UNAVAILABLE_NO_PREV_REPORT"):
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
