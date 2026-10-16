"""Compact, scan-first email layout for auction, open and close reports."""
from __future__ import annotations

import html
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from .extremes_board import build_extremes_board
from .limit_price import state_at_close
from .story_features import build_story_features
from .anomaly_detector import detect_anomalies
from .tradability_funnel import filter_candidates
from .story_composer import compose_story


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _short(value: Any, limit: int = 16) -> str:
    text = "—" if value is None else str(value).replace("\n", " ").replace("\r", " ")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _display_name(row: Mapping[str, Any], limit: int = 12) -> str:
    return _short(row.get("name") or "名称未知", limit)


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


def _amount_with_unit(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "金额未知"
    return f"{amount / 10_000:.0f}万" if 0 <= amount < 10_000_000 else f"{amount / 1e8:.1f}亿"


def _exact_price_state(row: Mapping[str, Any], *, prefix: str = "") -> str:
    return state_at_close(
        row.get(prefix + "close", row.get("close")),
        row.get(prefix + "high", row.get("high")),
        row.get(prefix + "low", row.get("low")),
        row.get("pre_close"), str(row.get("symbol", row.get("code", ""))),
        is_st=bool(row.get("is_st")),
        unlimited=bool(row.get("is_new_listing") or row.get("is_unlimited_price_band") or
                        row.get("unlimited_price_band")))


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [r for r in value if isinstance(r, Mapping)] if isinstance(value, list) else []


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    return next((row[k] for k in keys if row.get(k) is not None), None)


def _metric(label: str, value: str, color: str = "#f1f5f9") -> tuple[str, str]:
    return label, f'<td style="padding:7px 8px;background:{color};vertical-align:top"><div style="font-size:10px;color:#667085">{_esc(label)}</div><b style="display:block;padding-top:3px;font-size:14px">{_esc(value)}</b></td>'


def _tiles(report: Mapping[str, Any], stage: str) -> list[tuple[str, str]]:
    chapters = report.get("chapters") if isinstance(report.get("chapters"), Mapping) else {}
    terminal = report.get("auction_terminal_0925") if isinstance(report.get("auction_terminal_0925"), Mapping) else {}
    market_chapter = chapters.get("market_price_volume") if isinstance(chapters.get("market_price_volume"), Mapping) else {}
    market = report.get("market") if isinstance(report.get("market"), Mapping) else {}
    if not market:
        market = terminal if stage == "auction" and terminal else market_chapter
    market_valid = (market.get("status") not in {"STALE", "NOT_READY", "NOT_YET_AVAILABLE"}
                    and market.get("symbol_count") != 0)
    tiles: list[tuple[str, str]] = []
    median_key = "close_median" if stage == "close" else "p0945_median" if stage == "open" else "auction_median"
    median_label = "收盘中位" if stage == "close" else "09:45中位" if stage == "open" else "竞价中位"
    median_value = (_first(market, median_key, "median_pct_change" if stage != "close" else "close_median_pct")
                    if market_valid else None)
    if median_value is not None:
        tiles.append(_metric(median_label, _num(median_value)))
    up_share = _first(market, "up_share", "up_share_pct") if market_valid else None
    if up_share is not None and stage != "close":
        ratio = float(up_share) / 100 if float(up_share) > 1 else float(up_share)
        tiles.append(_metric("上涨占比", f"{ratio * 100:.1f}%"))
    up, down = (((market.get("auction_up_count"), market.get("auction_down_count")) if stage == "auction" else
                (_first(market, "open_up_count", "up_count"), _first(market, "open_down_count", "down_count")))
                if market_valid else (None, None))
    if up is not None or down is not None:
        tiles.append(_metric("涨/跌", f"{up if up is not None else '—'} / {down if down is not None else '—'}"))
    if stage != "close":
        limit_up, limit_down = ((_first(market, "limit_up", "auction_limit_up_count"),
                                 _first(market, "limit_down", "auction_limit_down_count"))
                                if market_valid else (None, None))
        if limit_up is not None or limit_down is not None:
            tiles.append(_metric("涨/跌停价代理", f"{limit_up if limit_up is not None else '—'} / {limit_down if limit_down is not None else '—'}"))
        amount = _first(market, "market_match_amount_yuan", "auction_amount_yuan") if market_valid else None
        if amount is not None:
            tiles.append(_metric("竞价匹配额" if stage == "auction" else "截至截点成交额", f"{_amount(amount)}亿"))
        yesterday = chapters.get("yesterday_highboards_summary") if isinstance(chapters.get("yesterday_highboards_summary"), Mapping) else {}
        pool = yesterday.get("yesterday_limit_pool") if isinstance(yesterday.get("yesterday_limit_pool"), Mapping) else {}
        pool_mean = pool.get("mean_pct_change")
        if pool_mean is not None:
            tiles.append(_metric("昨板竞价承接均值", _num(pool_mean)))
    if stage == "close" and (market.get("close_limit_up_count") is not None or market.get("close_limit_down_count") is not None):
        tiles.append(_metric("涨停价/跌停价", f"{market.get('close_limit_up_count', '—')} / {market.get('close_limit_down_count', '—')}"))
    stocks = _rows(report.get("stocks"))
    if stage == "close" and stocks and any("in_yest_pool" in row for row in stocks):
        pool = [row for row in stocks if row.get("in_yest_pool") is True or row.get("in_yest_pool") in (1, "1")]
        closes = [float(row["close_pct"]) for row in pool if isinstance(row.get("close_pct"), (int, float))]
        if closes:
            red = sum(value > 0 for value in closes) / len(closes)
            red_count = sum(value > 0 for value in closes)
            tiles.append(_metric("昨板表现", f"{_num(sum(closes) / len(closes))} / 红{red_count}/{len(closes)} ({red * 100:.1f}%)"))
        else:
            tiles.append(_metric("昨板表现", "数据缺口：昨日池无收盘值"))
    elif stage == "close":
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
    if not isinstance(auction_base, Mapping):
        baseline = market.get("vs_amount_baseline")
        if isinstance(baseline, Mapping):
            auction_base = {"multiple": baseline.get("multiple"), "percentile": baseline.get("percentile")}
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


def _story_event_text(event: Mapping[str, Any]) -> str:
    """Render an anomaly without leaking internal metric keys or float reprs."""
    supplied = event.get("summary") or event.get("text")
    if supplied:
        return f"{event.get('display_label', '对照观察')}：{supplied}"
    metric = str(event.get("metric") or event.get("id") or "")
    quality = str(event.get("quality") or "")
    value = event.get("value")
    label = {
        "close_up_share": "收盘广度", "breadth_low": "收盘广度",
        "close_median_pp": "收盘涨幅中位数", "auction_amount_yuan": "竞价匹配额",
        "auction_up_share": "竞价上涨占比", "auction_median_pct": "竞价涨幅中位数",
        "volume_day_change": "竞价匹配额", "ladder_high_promotion_rate": "高标晋级率",
        "theme_divergence": "版权板块日内变化",
    }.get(metric, str(event.get("display_name") or "市场对照"))
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = None
    if number is None:
        detail = "数据缺失"
    elif metric == "auction_up_share":
        detail = f"{number:.1%}"
    elif metric in {"close_up_share", "breadth_low"}:
        # A nested C201 input bundle may carry the denominator without copying it
        # onto every display event.
        nested = event.get("_narrative_facts")
        denominator = event.get("denominator") or (nested.get("fixed_daily_pool") if isinstance(nested, Mapping) else None)
        numerator = event.get("numerator")
        if denominator:
            detail = f"上涨{int(numerator) if numerator is not None else round(number * float(denominator))}只（{number:.1%}）"
        else:
            detail = f"上涨占比{number:.1%}"
    elif metric in {"volume_day_change"}:
        nested = event.get("_narrative_facts")
        amount = nested.get("auction_amount_yuan") if isinstance(nested, Mapping) else None
        amount_text = f"{float(amount) / 1e8:.2f}亿元；" if isinstance(amount, (int, float)) else ""
        detail = f"{amount_text}共同池为昨日{number:.2f}倍"
    elif metric == "auction_amount_yuan":
        detail = f"{number / 1e8:.2f}亿元"
    elif metric == "ladder_high_promotion_rate":
        detail = f"{number:.0%}"
    elif metric == "theme_divergence":
        detail = f"较竞价回升{number:+.2f}个百分点"
    elif "median" in metric or metric.endswith("_pp"):
        detail = f"{number:+.2f}个百分点"
    else:
        detail = f"{number:.2f}"
    ranks = event.get("ranks") if isinstance(event.get("ranks"), Mapping) else {}
    history_n = event.get("historical_n")
    if ranks.get("ascending") == 1 and isinstance(history_n, int):
        detail += f"，含当日{history_n + 1}日最低"
    elif ranks.get("descending") == 1 and isinstance(history_n, int):
        detail += f"，含当日{history_n + 1}日最高"
    baseline = event.get("baseline") or event.get("comparison_baseline")
    if not baseline and "FIXED_POOL_HISTORICAL" in quality:
        baseline = "固定池64日历史对照"
    elif not baseline and "MIXED_THRESHOLD_64D" in quality:
        baseline = "64日混阈值近似参照"
    n = event.get("historical_n", event.get("n"))
    if metric == "volume_day_change" and "64D" in quality:
        n = 64
    elif n is None and ("FIXED_POOL_HISTORICAL" in quality or "64D" in quality):
        n = 64
    context = []
    if baseline:
        context.append(str(baseline))
    if n is not None:
        context.append(f"n={n}")
    suffix = f"（{'；'.join(context)}）" if context else ""
    return f"{event.get('display_label', '对照观察')}：{label}{detail}{suffix}。"


def _story_text(story: Mapping[str, Any], report: Mapping[str, Any]) -> list[str]:
    lines = []
    headline = story.get("one_line_today") or report.get("one_line_today")
    if not headline:
        try:
            auction_share = float(report.get("auction_up_share"))
            peak_share = float(report.get("p0940_up_share"))
            late_share = float(report.get("p0945_up_share"))
            close_share = float(report.get("close_up_share"))
            close_median = float(report.get("close_median_pct"))
            late_median = float(report.get("p0945_median"))
        except (TypeError, ValueError):
            pass
        else:
            if auction_share < 0.5 and peak_share > late_share and close_share < late_share and close_median < late_median:
                headline = (f"竞价多数低开，早盘一度改善；收盘固定池上涨占比降至{close_share:.1%}，"
                           "局部回升没有扩展成全市场改善。")
    headline = headline or (story.get("headline") or {}).get("text")
    if headline:
        text = str(headline)
        if text.startswith("一句话今日："):
            lines.append(text)
        else:
            lines.append(f"一句话今日：{text}")
    story_events = story.get("top_events", [])[:3]
    if str(story.get("stage")) == "auction" and story_events:
        lines.append(f"今日{len(story_events)}件不一样的事：")
    for event in story_events:
        if isinstance(event, Mapping):
            display_event = dict(event)
            if isinstance(report.get("narrative_facts"), Mapping):
                display_event["_narrative_facts"] = report["narrative_facts"]
            if display_event.get("id") == "volume_day_change":
                display_event.setdefault("n", (report.get("narrative_facts") or {}).get("auction_prev_common_n"))
            lines.append(_story_event_text(display_event))
    acts = story.get("acts", [])
    stage_labels = {"auction": "第一幕｜竞价", "open": "第二幕｜开盘", "intraday": "第三幕｜盘中", "close": "第四幕｜收盘"}
    stage_order = ("auction", "open", "intraday", "close")
    current_stage = str(story.get("stage", "close"))
    visible_acts = [act for act in acts[:4] if isinstance(act, Mapping)
                    and (current_stage not in stage_order or str(act.get("stage")) in stage_order[:stage_order.index(current_stage) + 1])]
    for act in visible_acts:
        if not isinstance(act, Mapping) or act.get("status") == "UNOBSERVED" and not (act.get("lead") or {}).get("text"):
            continue
        stage = str(act.get("stage", ""))
        lead = str((act.get("lead") or {}).get("text") or "该阶段暂无可用观测。")
        no_afternoon = stage == "intraday" and str(story.get("stage")) == "close"
        if no_afternoon:
            lead = "下午连续路径缺失：封存输入止于09:45，不能补写午后过程。"
        lines.append(f"{stage_labels.get(stage, stage)}：{lead}")
        for item in ([] if no_afternoon else (act.get("evidence") or [])[:3]):
            if isinstance(item, Mapping) and item.get("text"):
                lines.append(f"证据：{item['text']}")
        transition_text = act.get("transition_text")
        transition = act.get("transition") or {}
        result = transition.get("observed_result") if isinstance(transition, Mapping) else None
        if transition_text:
            lines.append(f"转折：{transition_text}")
        elif result == "NOT_RETAINED_AT_CLOSE":
            lines.append("转折：早盘回升没有保持到收盘；两个端点不能还原下午过程。")
        elif result == "PARTIAL_RECOVERY":
            lines.append("转折：竞价留下的“上涨范围能否扩大”一度兑现，但09:45已回落。")
        elif result == "UNKNOWN_PATH":
            lines.append("留给收盘核对：早盘改善能否保持；下午过程没有连续记录。")
        elif stage == "close":
            try:
                late_share = float(report.get("p0945_up_share"))
                close_share = float(report.get("close_up_share"))
            except (TypeError, ValueError):
                pass
            else:
                if late_share > close_share:
                    lines.append("转折：早盘回升没有保持到收盘；两个端点不能还原下午过程。")
        if stage == "auction":
            try:
                narrative = report.get("narrative_facts") if isinstance(report.get("narrative_facts"), Mapping) else {}
                amount_ratio = float(report.get("auction_prev_common_ratio", narrative.get("auction_prev_common_ratio")))
            except (TypeError, ValueError):
                pass
            else:
                common_n = report.get("auction_prev_common_n", narrative.get("auction_prev_common_n"))
                sample = f"共同{int(common_n)}只" if common_n is not None else "共同样本"
                lines.append(f"和昨天比：{sample}竞价匹配额为昨日{amount_ratio:.2f}倍。")
            historical = report.get("auction_historical_comparison") or story.get("auction_historical_comparison")
            if not historical and any("MIXED_THRESHOLD_64D" in str(event.get("quality", "")) for event in story.get("top_events", []) if isinstance(event, Mapping)):
                historical = "64日混阈值近似参照（n=64），无可直接比较的历史倍数"
            if historical:
                lines.append(f"和往常比：{historical}。")
    funnel = story.get("funnel") or {}
    shown = funnel.get("shown") if isinstance(funnel.get("shown"), Mapping) else funnel
    own_facts = {}
    for row in report.get("candidate_facts", []):
        if isinstance(row, Mapping) and row.get("symbol"):
            own_facts[str(row["symbol"])] = row
    labels = (("watch", "能做"), ("wait", "要等"), ("exclude", "别碰"))
    chapters = report.get("chapters") if isinstance(report.get("chapters"), Mapping) else {}
    market = chapters.get("market_price_volume") if isinstance(chapters.get("market_price_volume"), Mapping) else {}
    market_unavailable = bool(market) and (
        market.get("status") in {"STALE", "NOT_READY", "NOT_YET_AVAILABLE"}
        or market.get("symbol_count") is None)
    for key, label in labels:
        entries = shown.get(key, [])
        if entries:
            for row in entries[:5]:
                if not isinstance(row, Mapping):
                    continue
                name = str(row.get("name") or "名称未知")
                condition = (row.get("condition") or {}).get("text", "")
                risk = str(row.get("risk") or "观察条件未验证")
                why = "；".join(str(x.get("text")) for x in row.get("reasons", []) if isinstance(x, Mapping) and x.get("text"))
                if why == condition:
                    why = ""
                fact = own_facts.get(str(row.get("symbol", "")), {})
                try:
                    own_pct = float(fact["p0945_pct"])
                    plate_pct = float(fact["plate0945"])
                    relative_pct = float(fact["relative0945"])
                except (KeyError, TypeError, ValueError):
                    pass
                else:
                    plate = str(fact.get("plate") or "所属板块")
                    individual = f"09:45个股{own_pct:+.1f}%，{plate}{plate_pct:+.1f}%，相对落后{abs(relative_pct):.1f}个百分点" if relative_pct < 0 else f"09:45个股{own_pct:+.1f}%，{plate}{plate_pct:+.1f}%，相对领先{relative_pct:.1f}个百分点"
                    why = "；".join(x for x in (why, individual) if x)
                    if fact.get("auction_price_status") == "INDICATIVE":
                        why += "；竞价价仍为指示价"
                    if row.get("bucket") != "exclude":
                        if relative_pct < 0:
                            condition = f"至少收复09:32价格，并缩小与{plate}的差距后再复核"
                        else:
                            condition = f"观察相对{plate}的领先能否延续到下一截点"
                        risk = "相对板块偏弱" if relative_pct < 0 else "相对强势尚未确认"
                details = "；".join(x for x in (why, str(condition), f"风险：{risk}") if x)
                lines.append(f"{label}：{name}｜{details}")
        elif key == "watch":
            if market_unavailable:
                lines.append("能做：当前截点数据不可用，候选数量和过滤原因未知。")
                continue
            counts = funnel.get("counts") or {}
            reason_counts = funnel.get("reason_counts") or {}
            candidates_n = sum(int(counts.get(k, 0) or 0) for k in ("watch", "wait", "exclude", "能做", "要等", "别碰"))
            why = "、".join(f"{reason} {count}只" for reason, count in list(reason_counts.items())[:3])
            if not why:
                why = "现有候选没有同时满足观察条件"
            lines.append(f"能做：暂无；既有候选{candidates_n}只未通过当前条件。主要原因：{why}。")
        elif key == "wait":
            lines.append("要等：当前截点数据不可用，候选数量未知。" if market_unavailable else
                         "要等：暂无；当前没有个股达到等待栏条件。")
        elif key == "exclude":
            lines.append("别碰：当前截点数据不可用，候选数量未知。" if market_unavailable else
                         "别碰：暂无；当前没有个股达到排除栏条件。")
    scope = story.get("scope_note") or story.get("data_gaps") or report.get("scope_note") or report.get("口径与缺口") or report.get("note")
    if not scope:
        quality = story.get("quality", [])
        known = []
        if any("AFTERNOON" in str(x) for x in quality): known.append("缺少09:45之后的连续路径")
        if any("AVAILABILITY" in str(x) for x in quality): known.append("历史可得时刻未知")
        if any("EXECUTION" in str(x) for x in quality): known.append("成交队列未知")
        scope = "；".join(known) or "四幕按各自可用截點统计；缺失项不按零处理。"
    lines.append(f"口径与缺口：{str(scope).replace('截點', '截点')}")
    return lines[:40]


def _story_view(report: Mapping[str, Any], stage: str, trade_date: str) -> tuple[dict[str, Any], str, str]:
    supplied = report.get("story")
    if isinstance(supplied, Mapping) and supplied.get("stage") == stage:
        story = dict(supplied)
    else:
        source = dict(report)
        nested_facts = report.get("narrative_facts")
        if isinstance(nested_facts, Mapping):
            source = {**nested_facts, **source}
        market = report.get("market") if isinstance(report.get("market"), Mapping) else {}
        source.update(market)
        source_facts = dict(source)
        timeline = source.get("market_timeline")
        if isinstance(timeline, list):
            for point in timeline:
                if not isinstance(point, Mapping):
                    continue
                tag = str(point.get("tag", ""))
                prefix = {"0925": "auction", "0932": "open", "0940": "p0940", "0945": "p0945", "close": "close"}.get(tag)
                if prefix:
                    source_facts[f"{prefix}_median_pct"] = point.get("median_pp")
                    source_facts[f"{prefix}_up_share"] = point.get("up_share")
                    source_facts[f"{prefix}_up_count"] = point.get("up_n")
                    source_facts[f"{prefix}_n"] = point.get("n")
        source_facts.setdefault("auction_amount_yuan", source.get("auction_amount_yuan"))
        source_facts.setdefault("auction_prev_common_ratio", source.get("auction_prev_common_ratio"))
        if stage == "auction":
            source = {"trade_date": trade_date,
                      "auction_amount_yuan": source.get("auction_amount_yuan", source.get("match_amount_yuan")),
                      "auction_up_share": source.get("auction_up_share", source.get("up_share")),
                      "auction_median_pct": source.get("auction_median_pct", source.get("median_pct")),
                      "auction_prev_common_ratio": source.get("auction_prev_common_ratio")}
        elif stage == "open":
            source = {"trade_date": trade_date, "open_median_pct": source.get("open_median_pct", source.get("p0932_median")),
                      "open_up_share": source.get("open_up_share", source.get("p0932_up_share")),
                      "p0945_median": source.get("p0945_median"), "p0945_up_share": source.get("p0945_up_share")}
        else:
            source = {"trade_date": trade_date, "close_median_pct": source.get("close_median_pct", source.get("close_median")),
                      "close_up_share": source.get("close_up_share", source.get("up_share")),
                      "close_amount_yuan": source.get("close_amount_yuan", source.get("amount_yuan"))}
        feature_facts = dict(source_facts)
        feature_facts["trade_date"] = trade_date
        features = build_story_features(feature_facts, stage=stage, cutoff=str(source.get("asof", stage)),
                                        observed_at=report.get("observed_at"),
                                        provenance={"source_fidelity": "UNKNOWN", "refs": ["email_report"]})
        candidates = []
        groups = report.get("a_to_b") if isinstance(report.get("a_to_b"), list) else []
        for group in groups:
            for row in group.get("candidates", []):
                candidates.append({**row, "bucket": row.get("bucket", "wait"),
                                   "condition": row.get("condition", ""),
                                   "risk": row.get("risk", "")})
        if not candidates and isinstance(source.get("funnel_display"), Mapping):
            for label, bucket in (("能做", "watch"), ("要等", "wait"), ("别碰", "exclude")):
                for row in source["funnel_display"].get(label, []):
                    if isinstance(row, Mapping):
                        candidates.append({**row, "bucket": bucket})
        funnel = filter_candidates(candidates, features, asof=str(source.get("asof") or stage))
        previous_acts = []
        prior_specs = (
            ("auction", "09:25", {"auction_median_pct": ("auction_median_pct", "auction_median"),
                                   "auction_up_share": ("auction_up_share", "auction_up_share"),
                                   "auction_amount_yuan": ("auction_amount_yuan", "match_amount_yuan"),
                                   "auction_prev_common_ratio": ("auction_prev_common_ratio",),
                                   "auction_n": ("auction_n",)}),
            ("open", "09:32", {"open_median_pct": ("open_median_pct", "p0932_median"),
                                "open_up_share": ("open_up_share", "p0932_up_share")}),
            ("intraday", "09:45", {"p0945_median": ("p0945_median",),
                                    "p0945_up_share": ("p0945_up_share",)}),
        )
        stage_order = ("auction", "open", "intraday", "close")
        for prior_stage, prior_cutoff, keys in prior_specs:
            if stage_order.index(prior_stage) >= stage_order.index(stage):
                break
            snapshot = {"trade_date": trade_date}
            for target, aliases in keys.items():
                value = next((source_facts[key] for key in aliases if source_facts.get(key) is not None), None)
                if value is not None:
                    snapshot[target] = value
            if len(snapshot) == 1:
                continue
            prior_features = build_story_features(snapshot, stage=prior_stage, cutoff=prior_cutoff,
                observed_at=None, provenance={"source_fidelity": "HISTORICAL_AVAILABILITY_UNKNOWN",
                                              "refs": ["email_report"]})
            prior_story = compose_story(prior_features, [], {"shown": {}}, previous_acts=[])
            previous_acts.extend(a for a in prior_story["acts"] if a["stage"] == prior_stage)
        story = compose_story(features, detect_anomalies([], []), funnel, previous_acts=previous_acts)
        event_rows = report.get("top_events", source_facts.get("top_events"))
        if isinstance(event_rows, list):
            story["top_events"] = event_rows[:3]
        claims = source_facts.get("story_claims")
        if isinstance(claims, Mapping) and claims.get("one_line_today"):
            story["one_line_today"] = str(claims["one_line_today"])
        story["scope_note"] = report.get("scope_note") or source_facts.get("scope_note")
    text_lines = _story_text(story, report)
    html_lines = "".join(f"<li>{_esc(line)}</li>" for line in text_lines)
    html_block = _section("今日三件不一样的事 · 四幕故事 · 观察漏斗",
                          f'<ol style="margin:0;padding-left:20px">{html_lines}</ol>',
                          "观察条件不构成买卖建议；缺失不按零处理。")
    return story, html_block, "\n".join(text_lines)


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
        if stage in {"auction", "open"}:
            prior_height = board - 1
            label = f"昨{prior_height}板 · 当前样本{len(items)}只"
        else:
            label = f"{board}板 昨{n if n is not None else '—'} 晋{k if k is not None else '—'}/{n if n is not None else '—'}"
        chips = []
        plain_chips = []
        for row in sorted(items, key=lambda r: (-float(r.get("auction_amount_yuan") or 0), str(r.get("symbol", ""))))[:12]:
            pct = row.get("close_pct") if stage == "close" else row.get("p0945_pct") if stage == "open" else _first(row, "p0925_pct", "auction_pct")
            limit = row.get("limit_pct")
            is_new = row.get("is_new_listing") or row.get("is_unlimited_price_band")
            if is_new:
                state = "新"
            elif stage == "close":
                state = _exact_price_state(row)
            elif stage == "open":
                state = (_exact_price_state(row, prefix="p0945_")
                         if any(row.get(f"p0945_{field}") is not None for field in ("open_pct", "high_pct", "low_pct", "close_pct"))
                         else "")
            else:
                state = "封" if row.get("auction_limit_state") in (1, "1", "UP") else ""
            if row.get("is_new_listing") or row.get("unlimited_price_band"):
                state = "新"
            name = _display_name(row, 8)
            chips.append(f'<span style="display:inline-block;margin:1px 3px 1px 0;padding:3px 5px;background:#f1f5f9;white-space:nowrap">{_esc(name)} {_esc(_num(pct))} {state} {_amount(row.get("auction_amount_yuan"))}亿</span>')
            plain_chips.append(f"{name} {_num(pct)} {state} {_amount(row.get('auction_amount_yuan'))}亿".strip())
        emphasis = "background:#fff1ed;padding:0 3px" if board == max(levels) else ""
        lines.append(f'<div style="line-height:23px"><b style="display:inline-block;min-width:94px;color:#9b3e32;{emphasis}">{_esc(label)}</b>{"".join(chips)}</div>')
        plain_lines.append(label + (" " + "、".join(plain_chips) if plain_chips else ""))
    firsts = [r for r in rows if _first(r, "prior_streak", "lb_days", "streak") in (0, "0")]
    firsts.sort(key=lambda r: (-float(r.get("auction_amount_yuan") or 0), str(r.get("symbol", ""))))
    if firsts:
        chips = "　".join(f"{_display_name(r, 8)} {_num(r.get('close_pct') if stage == 'close' else r.get('p0945_pct') if stage == 'open' else _first(r, 'p0925_pct', 'auction_pct'))}" for r in firsts[:8])
        lines.append(f'<div style="line-height:23px"><b>首板 {len(firsts)}只</b>　{_esc(chips)}</div>')
    gap_note = "" if data.get("promotion_status") != "MISSING_YESTERDAY_POOL" else "；数据缺口：缺昨日涨停池，晋级率不可算"
    space = f'<div style="font-size:10px;color:#667085;margin-top:3px">空间板 {max(levels) if levels else "—"}板；封/触为价格代理，非封单{gap_note}</div>'
    text = "连板梯队：\n" + "\n".join(plain_lines)
    return _section("连板梯队", "".join(lines) + space, "按昨日板数分层；封/触为价格代理，非封单") , text


def _extremes(report: Mapping[str, Any], stage: str) -> tuple[str, str]:
    attached = report.get("extremes_board")
    chapters = report.get("chapters") if isinstance(report.get("chapters"), Mapping) else {}
    if not isinstance(attached, Mapping):
        attached = chapters.get("extremes_board")
    if isinstance(attached, Mapping) and isinstance(attached.get(stage), list):
        attached = {**attached, stage: {"rows": attached[stage]}}
    if not isinstance(attached, Mapping):
        attached = build_extremes_board(_rows(report.get("stocks")), stage=stage)
    board = attached.get(stage) if isinstance(attached, Mapping) else None
    if isinstance(board, Mapping) and isinstance(board.get("rows"), list):
        blocks, plain = _selected_extremes(board, stage, report)
        exclusions = _rows((attached.get("auction") or {}).get("excluded_unlimited")) if isinstance(attached, Mapping) else []
        exclusions.extend(_rows(report.get("unlimited_price_band_stocks")))
        for key in ("big_meat", "big_loss"):
            exclusions.extend(row for row in _rows(report.get(key))
                              if row.get("is_new_listing") or row.get("is_unlimited_price_band") or
                              row.get("unlimited_price_band"))
        exclusions = list({str(row.get("symbol") or row.get("name")): row for row in exclusions}.values())
        if exclusions:
            names = "、".join(_display_name(row, 10) for row in exclusions[:8])
            note = "新股/无涨跌幅限制单列：" + names
            blocks += _section("价格带例外", f"<div>{_esc(note)}</div>")
            plain += "\n" + note
        return blocks, plain
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
            name = _display_name(row, 10)
            plate = _short(row.get("main_plate") or "—", 4)
            auction = _first(row, "auction_pct", "p0925_pct")
            p945 = row.get("p0945_pct") if stage != "auction" else None
            amount = _amount(_first(row, "auction_amount_yuan", "match_amount_yuan"))
            limit = row.get("limit_pct")
            close = row.get("close_pct")
            state = "新" if row.get("is_new_listing") or row.get("is_unlimited_price_band") else ""
            if stage == "close":
                state = _exact_price_state(row)
            elif stage == "open":
                state = _exact_price_state(row, prefix="p0945_")
            states.append(state)
            cells = [_esc(name), _esc(plate), _esc(_num(auction))]
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
        plain.append(f"{title}：" + "、".join(f"{_display_name(r, 8)} {_num(r.get('close_pct') if stage == 'close' else r.get('p0945_pct') if stage == 'open' else _first(r, 'auction_pct', 'p0925_pct'))}" for r in source))
    exceptional = list({str(r.get("symbol") or r.get("name")): r for r in exceptional}.values())
    if exceptional:
        names = "、".join(_display_name(r, 8) for r in exceptional[:8])
        note = f"新股/无涨跌幅限制：{names}"
        blocks.append(_section("价格带例外", f"<div>{_esc(note)}</div>"))
        plain.append(note)
    return "".join(blocks), "\n".join(plain)


def _selected_extremes(board: Mapping[str, Any], stage: str,
                       report: Mapping[str, Any]) -> tuple[str, str]:
    rows = _rows(board.get("rows"))
    headers = ["名称", "板块", "入选", "竞价%", "额(亿)"]
    if stage != "auction":
        headers.extend(["0932", "0935", "0940", "0945", "高/低Δ"])
    if stage == "open":
        headers.append("实时")
    if stage == "close":
        headers.extend(["收盘%", "Δ(pp)"])
    headers.append("状态")
    html_rows, text_rows = [], []
    for row in rows:
        track = row.get("tracking") if isinstance(row.get("tracking"), Mapping) else {}
        vals = [_display_name(row, 12), _short(row.get("main_plate") or "无板块", 8),
                row.get("selection_reason") or "—", _num(row.get("auction_pct")),
                _amount(row.get("auction_amount_yuan"))]
        if stage != "auction":
            vals.extend([_num(row.get("p0932_pct")), _num(row.get("p0935_pct")),
                         _num(row.get("p0940_pct")), _num(row.get("p0945_pct")),
                         f"{_num(track.get('high_vs_auction_pp'))}/{_num(track.get('low_vs_auction_pp'))}"])
        if stage == "open":
            vals.append(_num(track.get("latest_pct")))
        if stage == "close":
            vals.extend([_num(track.get("close_pct")), _num(track.get("delta_close_pp"))])
        state = row.get("auction_state") or row.get("auction_price_status") or "—"
        if stage == "close" and not track.get("close_pct") and track.get("close_pct") is None:
            state = (state + "；" if state != "—" else "") + "收盘缺失"
        vals.append(state)
        html_rows.append("<tr>" + "".join(f'<td style="padding:4px 5px;white-space:nowrap;border-bottom:1px solid #e8edf2">{_esc(value)}</td>' for value in vals) + "</tr>")
        text_rows.append(" | ".join(str(value) for value in vals))
    th = "".join(f'<th style="text-align:left;padding:4px 5px;white-space:nowrap">{_esc(value)}</th>' for value in headers)
    table = f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-size:11px"><thead><tr>{th}</tr></thead><tbody>{"".join(html_rows)}</tbody></table>'
    title = {"auction": "竞价先选极值集合", "open": "竞价集合开盘追踪", "close": "竞价集合收盘追踪"}[stage]
    note = "固定集合由≤09:25竞价绝对涨幅前20与金额前20并集；金额阈值3000万元；当日描述。"
    if stage == "close":
        note += " 兑现/修复与回落/走弱榜仅在该集合内排序。"
    if stage == "open":
        note += " 开盘追踪点为0932、09:45及当前可得实时值。"
    legend = str(board.get("rank_legend") or "绝对值#=竞价涨跌幅绝对值排名；额#=竞价匹配额排名。")
    note += " " + legend
    blocks = [_section(title, table if rows else "<p>缺少符合门槛的竞价样本。</p>", note)]
    plain = [title + ("：\n" + "\n".join(text_rows) if text_rows else "：缺少符合门槛的竞价样本。"), legend]
    if stage != "auction":
        delta_key = "delta_latest_pp" if stage == "open" else "delta_close_pp"
        endpoint_label = "实时" if stage == "open" else "收盘"
        group_label = "09:45" if stage == "open" else "收盘"
        for label, key in (("兑现/修复 Top8", "rebound_top8"), ("回落/走弱 Top8", "weak_top8")):
            names = [f"{_display_name(row, 10)} {_num((row.get('tracking') or {}).get(delta_key)).rstrip('%')}pp"
                     for row in _rows(board.get(key))]
            line = label + "（固定集合内）: " + ("、".join(names) or "—")
            blocks.append(f"<div>{_esc(line)}</div>")
            plain.append(line)
        for label, key in (("竞价高开组", "high_open_group"),
                           ("竞价低开组", "low_open_group"),
                           ("额前20组", "amount_top20")):
            stats = board.get(key) if isinstance(board.get(key), Mapping) else {}
            average = stats.get("amount_weighted_pct") if key == "amount_top20" else stats.get("mean_pct")
            metric_label = f"{group_label}额权均值" if key == "amount_top20" else f"{group_label}均值"
            line = f"{label} {metric_label} {_num(average)}，红盘 {stats.get('red', 0)}/{stats.get('n', 0)}"
            blocks.append(f"<div>{_esc(line)}</div>")
            plain.append(line)
    if stage == "close":
        legacy = []
        for key, label in (("big_meat", "旧大肉"), ("big_loss", "旧大面")):
            legacy.extend(f"{label} {_display_name(row, 10)}" for row in _rows(report.get(key))[:5])
        if legacy:
            line = "旧全市场收盘极值附录（事后筛选，非可见机会；旧大肉 TOP8/旧大面 TOP8 候选各展示Top5）：" + "、".join(legacy)
            blocks.append(_section("旧口径事后极值附录", f"<div>{_esc(line)}</div>"))
            plain.append(line)
    exceptional = _rows(report.get("unlimited_price_band_stocks"))
    exceptional.extend(row for row in rows if row.get("is_new_listing") or row.get("is_unlimited_price_band"))
    unique_exceptional = list({str(row.get("symbol") or row.get("name")): row for row in exceptional}.values())
    if unique_exceptional:
        names = "、".join(_display_name(row, 10) for row in unique_exceptional[:8])
        line = "新股/无涨跌幅限制单列：" + names
        blocks.append(_section("价格带例外", f"<div>{_esc(line)}</div>"))
        plain.append(line)
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
    chapters = report.get("chapters") if isinstance(report.get("chapters"), Mapping) else {}
    data = report.get("plates") if isinstance(report.get("plates"), Mapping) else {}
    market = chapters.get("market_price_volume") if isinstance(chapters.get("market_price_volume"), Mapping) else {}
    if stage == "auction" and not data.get("auction_amount_top10"):
        data = {**data, "auction_amount_top10": [
            {"name": row.get("name"), "members": row.get("member_count"),
             "auction_amount_yuan": row.get("match_amount_yuan"),
             "auction_amount_share_bp": row.get("market_share_bp"),
             "p0925_mean": row.get("equal_weight_pct"), "kind": "竞价额份额"}
            for row in _rows(market.get("top_plates_by_share"))]}
    if stage != "auction" and not data.get("narrow_theme_top5"):
        quadrant = chapters.get("plate_quadrants") if isinstance(chapters.get("plate_quadrants"), Mapping) else {}
        qrows = sorted(_rows(quadrant.get("rows")), key=lambda r: float(r.get("amount_yuan") or 0), reverse=True)
        data = {**data, "narrow_theme_top5": [
            {"name": row.get("name"), "members": row.get("member_count"),
             "auction_amount_yuan": row.get("amount_yuan"), "current_mean": row.get("our_pct"),
             "kind": row.get("quadrant"), "flags": row.get("flags")}
            for row in qrows[:8]]}
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
            p0932_keys = [r.get("p0932_pct") for r in members if isinstance(r.get("p0932_pct"), (int, float))]
            p0945_keys = [r.get("p0945_pct") for r in members if isinstance(r.get("p0945_pct"), (int, float))]
            close_keys = [r.get("close_pct") for r in members if isinstance(r.get("close_pct"), (int, float))]
            narrow.append({"name": name, "kind": kind, "members": len(members),
                           "auction_amount_yuan": amount,
                           "auction_amount_share": amount / market_total if market_total else None,
                           "p0925_mean": sum(pct_keys) / len(pct_keys) if pct_keys else None,
                           "p0932_mean": sum(p0932_keys) / len(p0932_keys) if p0932_keys else None,
                           "p0945_mean": sum(p0945_keys) / len(p0945_keys) if p0945_keys else None,
                           "p0932_coverage": len(p0932_keys) / len(members),
                           "p0945_coverage": len(p0945_keys) / len(members),
                           "close_mean": sum(close_keys) / len(close_keys) if close_keys else None})
        narrow.sort(key=lambda r: (-(r.get("auction_amount_share") or 0), r["name"]))
        narrow = narrow[:5]
    chosen = [(r, "主线") for r in top] + [(r, "窄主题") for r in narrow] + [(r, "弱势") for r in weak]
    if not chosen:
        msg = "数据缺口：板块事实未生成"
        return _section("板块盘面", f'<div>{msg}</div>'), msg
    headers = (["类", "板块", "人数", "竞价额", "占比bp", "竞价%"] if stage == "auction" else
               ["盘面", "板块", "人数", "当前成员额", "份额bp", "当前等权%"] if stage == "open" else
               ["类", "板块", "人数", "竞价额", "占比bp", "竞价%"])
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
        pct = _first(row, "p0925_mean", "auction_mean", "mean_pct", "pct") if stage == "auction" else _first(row, "current_mean", "our_pct", "p0945_mean", "mean_pct")
        if stage == "close" and isinstance(row.get("trend"), list):
            values = row["trend"]
        elif stage == "auction":
            values = [row.get("p0920_mean"), row.get("p0924_mean"), pct]
        else:
            values = [row.get("p0932_mean"), row.get("p0935_mean"), row.get("p0940_mean"),
                      row.get("p0945_mean"), row.get("close_mean")]
        values = values[:3] if stage == "auction" else values[:3] if stage == "open" else values
        if stage == "open" and sum(isinstance(value, (int, float)) for value in values) < 2:
            values = []
        cells = [kind, _short(row.get("name") or row.get("plate") or "—", 8), str(_first(row, "members", "count") or "—"), _amount(amount), f"{float(share):.0f}" if isinstance(share, (int, float)) else "—", _num(pct)]
        if stage == "close":
            cells.append(_num(row.get("close_mean")))
        cells.append(_spark(values))
        bg = "#fff" if index % 2 == 0 else "#f7f9fb"
        table_rows.append("<tr>" + "".join(f'<td style="padding:4px 5px;background:{bg};white-space:nowrap;border-bottom:1px solid #e8edf2">{_esc(c)}</td>' for c in cells) + "</tr>")
        if kind == "主线" and isinstance(share, (int, float)):
            notes.append(f"{_short(row.get('name'), 8)} {share:.0f}bp")
        elif stage != "close":
            notes.append(f"{_short(row.get('name') or row.get('plate'), 8)} {_num(pct)}")
    th = "".join(f'<th style="text-align:left;padding:4px 5px;background:#e9eef5;white-space:nowrap">{_esc(h)}</th>' for h in headers)
    table = f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-size:11px"><thead><tr>{th}</tr></thead><tbody>{"".join(table_rows)}</tbody></table>'
    note = ("额单位：亿元；份额单位：bp且成员重叠不可加总；变化列没有可比历史切片时留空。"
            if stage == "auction" else "额单位：亿元；份额单位：bp且成员重叠不可加总；当前成员额为板块聚合口径，板块间成员可重叠。")
    return _section("板块盘面", table, note), "板块盘面：" + "；".join(notes)


def _a_to_b(report: Mapping[str, Any], stage: str) -> tuple[str, str]:
    entries = _rows(report.get("a_to_b"))
    if not isinstance(report.get("a_to_b"), list):
        msg = "数据缺口：未生成看A做B"
        return _section("看A做B", f'<div>{msg}</div>'), msg
    if not entries:
        msg = "看A做B：无合格A"
        return _section("看A做B", f'<div>{msg}</div>'), msg
    if any(isinstance(entry, Mapping) and entry.get("schema_version") == 2 for entry in entries):
        return _a_to_b_v2(report, entries, stage)
    output, plain = [], []
    for entry in entries[:8]:
        entry = dict(entry)
        anchor_stock = next((row for row in _rows(report.get("stocks"))
                             if str(row.get("symbol", "")).zfill(6) == str(entry.get("a_symbol", "")).zfill(6)), {})
        if entry.get("a_auction_pct") is None:
            entry["a_auction_pct"] = _first(anchor_stock, "p0925_pct", "auction_pct")
        if entry.get("a_close_pct") is None:
            entry["a_close_pct"] = anchor_stock.get("close_pct")
        anchor_name = _short(entry.get('a_name') or entry.get('name') or "名称未知", 8)
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
            text = f"B {_display_name(row, 8)} {_num(_first(row, 'auction_pct', 'p0925_pct'))} {_amount_with_unit(row.get('auction_amount_yuan'))} {_position_label(row)}"
            if row.get("shared_anchor_names"):
                text += " 共同同伴"
            if stage != "auction":
                text += f" {_num(row.get('p0945_pct'))}"
            if stage == "close":
                text += f" {_num(row.get('close_pct'))}"
            chips.append(text)
        reason_map = {"无合格主题板块": "无合格板块", "无板块关系": "无板块关系", "无合格同伴": "无同伴",
                      "同伴全部买不到": "同伴买不到", "缺少竞价价/额": "缺竞价数据"}
        reason = "、".join(chips) if chips else reason_map.get(str(entry.get("reason") or ""), _short(entry.get("reason") or "无同伴", 10))
        content = f'<div style="line-height:23px"><b>{_esc(anchor)}</b><div style="padding-left:8px">{_esc(reason)}</div></div>'
        output.append(content)
        plain.append(anchor + " → " + reason)
    return _section("看A做B", "".join(output), "点时同板块候选；不构成建议"), "看A做B：\n" + "\n".join(plain)


def _a_to_b_v2(report: Mapping[str, Any], entries: Sequence[Mapping[str, Any]], stage: str) -> tuple[str, str]:
    def pct(value):
        return "—" if value is None else f"{float(value):+.1f}%"

    def yi(value):
        return "—" if value is None else f"{float(value) / 1e8:.2f}亿"

    def val(value, digits=1):
        return "—" if value is None else f"{float(value):.{digits}f}"

    def pp(value):
        return "—" if value is None else f"{float(value):+.1f}pp"

    def path_history(value):
        if not isinstance(value, Mapping):
            return "样本不足/未知"
        k, n = value.get("k"), value.get("n")
        days = value.get("days", value.get("sample_days"))
        if isinstance(k, (int, float)) and isinstance(n, (int, float)) and n >= 30 and (days or 0) >= 5:
            return f"14日研究频率 {int(k)}/{int(n)}（{days}日）"
        return f"样本不足 {k if k is not None else '—'}/{n if n is not None else '—'}（{days if days is not None else '—'}日）"

    def sentiment_history(sentiment):
        value = sentiment.get("history_frequency")
        if not isinstance(value, Mapping):
            return "样本不足/未知"
        k, n = value.get("k"), value.get("n")
        days = value.get("days", value.get("sample_days"))
        if k is None or n is None:
            return "样本不足/未知"
        return f"{k}/{n}（{days if days is not None else n}日样本）"

    no_candidate_labels = {
        "无合格主题板块": "无合格板块", "无板块关系": "无合格板块",
        "无同伴": "无同伴", "无合格同伴": "无同伴",
        "同伴全部买不到": "同伴均买不到", "缺少竞价价/额": "缺竞价数据",
        "情绪档不支持（描述性）": "情绪档不支持（描述性）",
    }

    names = {str(row.get("symbol", row.get("code", ""))).zfill(6): row.get("name")
             for row in _rows(report.get("stocks"))}
    html_blocks, text_blocks = [], []
    for entry in entries:
        strength = entry.get("a_strength") or {}
        plate = entry.get("plate_state") or {}
        sentiment = entry.get("sentiment_state") or {}
        name = _short(entry.get("a_name") or names.get(str(entry.get("a_symbol", "")).zfill(6)) or "名称未知", 10)
        streak = entry.get("a_prior_streak")
        status = str(entry.get("a_status") or "—")
        plate_name = _short(plate.get("name") or entry.get("plate") or "无合格板块", 12)
        today = entry.get("a_today_status") or "—"
        proxy = "（价格代理）" if entry.get("a_status_is_price_proxy") else ""
        title = f"A {name} · 昨{streak if streak is not None else '—'}板→今日{today}{proxy} · 板块:{plate_name}({plate.get('member_count') if plate.get('member_count') is not None else '—'}人,{plate.get('class') or '类别未知'})"
        track = strength.get("price_track_pct") or {}
        track_keys = ["0925"] if stage == "auction" else ["0925", "0932", "0945"]
        if stage == "close": track_keys.append("close")
        trajectory = "→".join(f"{'收盘' if key == 'close' else key} {pct(track.get(key))}" for key in track_keys)
        amount_rank = strength.get("auction_amount_rank")
        same_count, same_rank = strength.get("same_streak_count"), strength.get("same_streak_rank")
        metrics = [
            f"A强度：竞价 {pct(strength.get('auction_pct'))}；竞价额 {yi(strength.get('auction_amount_yuan'))}，占大盘 {val(strength.get('auction_amount_bp'))}bp，金额全市场第 {amount_rank if amount_rank is not None else '—'}（{'前20' if isinstance(amount_rank, int) and amount_rank <= 20 else '非前20' if amount_rank is not None else '排名未知'}）",
            f"封单/匹配额：{val(strength.get('seal_to_match_ratio') * 100 if strength.get('seal_to_match_ratio') is not None else None)}%；轨迹 {trajectory}；日高低 {pct(strength.get('high_pct'))}/{pct(strength.get('low_pct'))}；路径 {strength.get('path_type') or 'UNKNOWN'}",
            f"同身位竞价额：{same_rank if same_rank is not None else '—'}/{same_count if same_count is not None else '—'}；{path_history(strength.get('path_frequency'))}",
            "选A原因：" + ("；".join(f"{r.get('rule_id')} {r.get('text')}" for r in entry.get("a_reasons", [])) or "规则原因缺失"),
            "板块状态：竞价额 " + yi(plate.get('auction_amount_yuan')) + "，占大盘 " + val(plate.get('auction_amount_share_bp')) + "bp（重叠不可加）"
            + (f"；对64日/前日 {val(plate.get('relative_64d_multiple'))}/{val(plate.get('relative_prev_multiple'))}倍" if plate.get('relative_64d_multiple') is not None or plate.get('relative_prev_multiple') is not None else "")
            + f"；成员上涨占比 {val(plate.get('up_share') * 100 if plate.get('up_share') is not None else None)}%"
            + f"；等权涨幅 {pct(plate.get('equal_weight_pct'))}→{pct(plate.get('equal_weight_0945_pct'))}"
            + (f"→{pct(plate.get('equal_weight_close_pct'))}" if stage == "close" else "")
            + (f"；涨停价 {plate.get('limit_up_count')}只" if plate.get('limit_up_count') is not None else "")
            + ("；当日强势板块" if plate.get('strength_top') is True else "；当日弱势板块" if plate.get('weak_top') is True else "")
            + f"；关系日期 {plate.get('relation_asof') or '未知'}"
            + (f"（滞后{plate.get('relation_lag_trading_days')}个交易日）" if plate.get('relation_lag_trading_days') is not None else ""),
            f"情绪档：{ {'WEAK': '弱势', 'STRONG': '强势', 'NEUTRAL': '中性', 'PANIC': '恐慌', 'REPAIR': '修复'}.get(str(sentiment.get('label')), sentiment.get('label') or '未知') }；历史频率 {sentiment_history(sentiment)}；单日描述，非预测",
        ]
        candidates = entry.get("b_candidates") or []
        if candidates:
            for b in candidates[:3]:
                bm = b.get("metrics") or {}
                reason = "、".join(b.get("reasons") or []) or "入选原因缺失"
                buyable = "买得到✓" if b.get("buyable") else "买不到"
                position = _position_label(bm)
                position_value = _first(bm, "prior_range_position20_pct", "p0925_range_position20_pct")
                position_text = (f"前20日区间位置 {float(position_value):.0f}%" if isinstance(position_value, (int, float))
                                 else "前20日区间位置未知" if position == "位置未知"
                                 else f"前20日区间位置 {position}")
                line = (f"B { _short(b.get('name') or '名称未知', 10) } | {reason} | {pct(bm.get('auction_pct'))} | "
                        f"{yi(bm.get('auction_amount_yuan'))}/板块额{pct((bm.get('plate_amount_share') or 0) * 100 if bm.get('plate_amount_share') is not None else None)} | "
                        f"{position_text} | {buyable}（竞价未涨停/非接近涨跌停）")
                if stage != "auction":
                    line += f" | 0945相对板块 {pp(bm.get('relative_to_plate_0945_pp'))}"
                if stage == "close":
                    line += f" | 收盘相对板块 {pp(bm.get('relative_to_plate_close_pp'))}"
                metrics.append(line)
        else:
            reason = str(entry.get("reason") or "无同伴")
            metrics.append("B：" + no_candidate_labels.get(reason, reason))
        metrics.append(f"依据与限制：关系日期 {plate.get('relation_asof') or 'UNKNOWN'}；路径研究14日描述性；非投资建议。排序：竞价额降序，同额按共同板块数。")
        html_blocks.append('<div style="line-height:20px;white-space:nowrap;overflow-x:auto;border-bottom:1px solid #ddd;padding:5px 0">' +
                           "".join(f"<div>{_esc(line)}</div>" for line in [title, *metrics]) + "</div>")
        text_blocks.append("\n".join([title, *metrics]))
    plain = "看A做B：\n" + "\n\n".join(text_blocks)
    return _section("看A做B", "".join(html_blocks), "单日描述；缺失保留；不构成建议"), plain


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


def _other(report: Mapping[str, Any], stage: str) -> tuple[str, str]:
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
    chapters = report.get("chapters") if isinstance(report.get("chapters"), Mapping) else {}
    market = report.get("market") if isinstance(report.get("market"), Mapping) else {}
    chapter_market = chapters.get("market_price_volume") if isinstance(chapters.get("market_price_volume"), Mapping) else {}
    if (not isinstance(market.get("auction_baseline_comparison"), Mapping) and market.get("auction_baseline_multiple") is None
            and not isinstance(chapter_market.get("auction_baseline_comparison"), Mapping)
            and chapter_market.get("auction_baseline_multiple") is None):
        gaps.append("竞价额历史基线未生成")
    yesterday = report.get("yesterday_review")
    has_pool = any("in_yest_pool" in row for row in _rows(report.get("stocks")))
    yesterday_summary = chapters.get("yesterday_highboards_summary")
    if (not has_pool and not isinstance(yesterday_summary, Mapping)
            and (not isinstance(yesterday, Mapping) or yesterday.get("status") == "UNAVAILABLE_NO_PREV_REPORT")):
        gaps.append("昨板回看缺前日报告")
    unique = list(dict.fromkeys(gaps))[:3]
    msg = "；".join(unique) if unique else "缺失不补零；仅描述当日事实"
    scorecard = (report.get("emerging_scorecard")
                 if stage == "close" and isinstance(report.get("emerging_scorecard"), Mapping) else {})
    summary = []
    for key, label in (("emerging", "新冒头"), ("p0945_top_k", "涨幅前K"), ("amount_top_k", "金额前K")):
        cohort = scorecard.get(key)
        if isinstance(cohort, Mapping):
            summary.append(f"{label} {cohort.get('n', '—')}只；收盘≥5% {cohort.get('close_ge_5_count', '—')}；涨停价代理 {cohort.get('sealed_up_count', '—')}；转负 {cohort.get('turned_negative_count', '—')}")
    score_text = "新冒头记分卡（收盘涨停为价格代理，封单未知）：" + "；".join(summary) if summary else ""
    content = (f'<div>{_esc(score_text)}</div>' if score_text else "") + f'<div>{_esc(msg)}</div>'
    return _section("新冒头记分卡 / 数据与口径", content), (score_text + "\n" if score_text else "") + msg


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
    other_html, other_text = _other(report, stage_key)
    _story, story_html, story_text = _story_view(report, stage_key, trade_date)
    appendix_html = (f'<section style="margin:12px 0"><h2 style="font-size:13px;color:#475467">附表：完整事实章节</h2>'
                     f'{_section("情绪温度计", tile_html)}{ladder_html}{extreme_html}{plate_html}{ab_html}{other_html}</section>')
    body = (f'<div style="max-width:680px;margin:0 auto;background:#f5f7fa;color:#263445;font-family:Arial,"Microsoft YaHei",sans-serif;font-size:12px;line-height:1.45;font-variant-numeric:tabular-nums">'
            f'<header style="padding:10px 2px"><b>{_esc(trade_date)} {stage_label}盘面</b></header>'
            f'{story_html}{appendix_html}'
            '<footer style="padding:8px;color:#667085;font-size:10px">缺失不补零；涨停状态为价格代理；单日事实不构成预测或建议。</footer></div>')
    if "close" != stage_key:
        # The A→B chapter must not leak later prices into an earlier stage.
        for forbidden in ("close_pct", "close_mean", "close_median", "收盘%", "收盘中位", "收盘"):
            assert forbidden not in ab_html
            assert forbidden not in ab_text
    document = '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="margin:0;background:#f5f7fa">' + body + "</body></html>"
    tile_text = []
    for label, markup in tiles:
        value = html.unescape(re.sub(r"<[^>]*>", "", markup)).strip()
        if value.startswith(label):
            value = value[len(label):].strip()
        tile_text.append(f"{label} {value}".strip())
    plain = (f"{trade_date} {stage_label}盘面\n{story_text}\n\n附表：完整事实章节\n情绪温度计：" +
             "；".join(tile_text) +
             f"\n{ladder_text}\n{extreme_text}\n{plate_text}\n{ab_text}\n数据与口径：{other_text}")
    return document, plain
