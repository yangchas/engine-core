"""Compact, scan-first email layout for auction, open and close reports."""
from __future__ import annotations

import html
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .extremes_board import build_extremes_board
from .live_session import OPEN_CHECKPOINTS
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


def _open_checkpoint_label(report: Mapping[str, Any]) -> str:
    value = report.get("checkpoint")
    if isinstance(value, str) and value in {"0925", "0932", "0935", "0938", "0940", "0945"}:
        return value
    from .live_session import open_checkpoint_for_asof
    return open_checkpoint_for_asof(report.get("asof"))


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
    checkpoint = _open_checkpoint_label(report) if stage == "open" else None
    median_key = "close_median" if stage == "close" else f"p{checkpoint}_median" if stage == "open" else "auction_median"
    median_label = ("收盘中位" if stage == "close" else
                    f"{checkpoint[:2]}:{checkpoint[2:]}中位" if stage == "open" else "竞价中位")
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
    checkpoint = _open_checkpoint_label(report) if stage == "open" else "0925"
    current_pct_key = f"p{checkpoint}_pct"
    current_prefix = f"p{checkpoint}_"
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
            pct = row.get("close_pct") if stage == "close" else _first(row, "current_pct", current_pct_key) if stage == "open" else _first(row, "p0925_pct", "auction_pct")
            limit = row.get("limit_pct")
            is_new = row.get("is_new_listing") or row.get("is_unlimited_price_band")
            if is_new:
                state = "新"
            elif stage == "close":
                state = _exact_price_state(row)
            elif stage == "open":
                state = (_exact_price_state(row, prefix=current_prefix)
                         if any(row.get(f"{current_prefix}{field}") is not None for field in ("open_pct", "high_pct", "low_pct", "close_pct"))
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
        chips = "　".join(f"{_display_name(r, 8)} {_num(r.get('close_pct') if stage == 'close' else _first(r, 'current_pct', current_pct_key) if stage == 'open' else _first(r, 'p0925_pct', 'auction_pct'))}" for r in firsts[:8])
        lines.append(f'<div style="line-height:23px"><b>首板 {len(firsts)}只</b>　{_esc(chips)}</div>')
    gap_note = "" if data.get("promotion_status") != "MISSING_YESTERDAY_POOL" else "；数据缺口：缺昨日涨停池，晋级率不可算"
    space = f'<div style="font-size:10px;color:#667085;margin-top:3px">空间板 {max(levels) if levels else "—"}板；封/触为价格代理，非封单{gap_note}</div>'
    text = "连板梯队：\n" + "\n".join(plain_lines)
    return _section("连板梯队", "".join(lines) + space, "按昨日板数分层；封/触为价格代理，非封单") , text


def _extremes(report: Mapping[str, Any], stage: str, limit: int | None = None) -> tuple[str, str]:
    checkpoint = _open_checkpoint_label(report) if stage == "open" else "0925"
    current_pct_key = f"p{checkpoint}_pct"
    current_prefix = f"p{checkpoint}_"
    attached = report.get("extremes_board")
    chapters = report.get("chapters") if isinstance(report.get("chapters"), Mapping) else {}
    if not isinstance(attached, Mapping):
        attached = chapters.get("extremes_board")
    if isinstance(attached, Mapping) and isinstance(attached.get(stage), list):
        attached = {**attached, stage: {"rows": attached[stage]}}
    if not isinstance(attached, Mapping):
        attached = build_extremes_board(_rows(report.get("stocks")), stage=stage,
                                        checkpoint=checkpoint if stage == "open" else None)
    board = attached.get(stage) if isinstance(attached, Mapping) else None
    if isinstance(board, Mapping) and isinstance(board.get("rows"), list):
        blocks, plain = _selected_extremes(board, stage, report, limit)
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
        key = "p0925_pct" if stage == "auction" else current_pct_key
        title_up = "竞价强 TOP8" if stage == "auction" else f"{checkpoint}强 TOP8"
        title_down = "竞价弱 TOP8" if stage == "auction" else f"{checkpoint}弱 TOP8"
        source_rows = _rows(report.get("stocks"))
        if not source_rows:
            chapters = report.get("chapters") if isinstance(report.get("chapters"), Mapping) else {}
            source_rows = _rows(chapters.get("yesterday_highboards"))
        values = [r for r in source_rows if not (r.get("is_new_listing") or r.get("is_unlimited_price_band") or r.get("unlimited_price_band"))
                  and isinstance(_first(r, "current_pct", key, "auction_pct" if stage == "auction" else current_pct_key), (int, float))]
        groups = [("__stage_up", title_up), ("__stage_down", title_down)]
        report = dict(report)
        report["__stage_up"] = sorted(values, key=lambda r: -float(_first(r, "current_pct", key, "auction_pct" if stage == "auction" else current_pct_key)))
        report["__stage_down"] = sorted(values, key=lambda r: float(_first(r, "current_pct", key, "auction_pct" if stage == "auction" else current_pct_key)))
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
            headers.append(f"{checkpoint[:2]}:{checkpoint[2:]}")
        headers.append("额")
        if stage == "close":
            headers.insert(4, "收盘%")
        body = []
        states = []
        for index, row in enumerate(source):
            name = _display_name(row, 10)
            plate = _short(row.get("main_plate") or "—", 4)
            auction = _first(row, "auction_pct", "p0925_pct")
            p945 = _first(row, "current_pct", current_pct_key) if stage == "open" else row.get("p0925_pct") if stage == "auction" else None
            amount = _amount(_first(row, "auction_amount_yuan", "match_amount_yuan"))
            limit = row.get("limit_pct")
            close = row.get("close_pct")
            state = "新" if row.get("is_new_listing") or row.get("is_unlimited_price_band") else ""
            if stage == "close":
                state = _exact_price_state(row)
            elif stage == "open":
                state = _exact_price_state(row, prefix=current_prefix)
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
        plain.append(f"{title}：" + "、".join(f"{_display_name(r, 8)} {_num(r.get('close_pct') if stage == 'close' else _first(r, 'current_pct', current_pct_key) if stage == 'open' else _first(r, 'auction_pct', 'p0925_pct'))}" for r in source))
    exceptional = list({str(r.get("symbol") or r.get("name")): r for r in exceptional}.values())
    if exceptional:
        names = "、".join(_display_name(r, 8) for r in exceptional[:8])
        note = f"新股/无涨跌幅限制：{names}"
        blocks.append(_section("价格带例外", f"<div>{_esc(note)}</div>"))
        plain.append(note)
    return "".join(blocks), "\n".join(plain)


def _selected_extremes(board: Mapping[str, Any], stage: str,
                       report: Mapping[str, Any], limit: int | None = None) -> tuple[str, str]:
    rows = _rows(board.get("rows"))
    omitted = max(0, len(rows) - limit) if limit is not None else 0
    displayed_rows = rows[:limit] if limit is not None else rows
    checkpoint = _open_checkpoint_label(report) if stage == "open" else "0945"
    headers = ["名称", "板块", "入选", "竞价%", "额(亿)"]
    if stage != "auction":
        visible = [label for label, _ in OPEN_CHECKPOINTS if label <= checkpoint]
        headers.extend(visible + ["高/低Δ"])
    if stage == "open":
        headers.append("实时")
    if stage == "close":
        headers.extend(["收盘%", "Δ(pp)"])
    headers.append("状态")
    html_rows, text_rows = [], []
    for row in displayed_rows:
        track = row.get("tracking") if isinstance(row.get("tracking"), Mapping) else {}
        vals = [_display_name(row, 12), _short(row.get("main_plate") or "无板块", 8),
                row.get("selection_reason") or "—", _num(row.get("auction_pct")),
                _amount(row.get("auction_amount_yuan"))]
        if stage != "auction":
            visible = [label for label, _ in OPEN_CHECKPOINTS if label <= checkpoint]
            vals.extend([_num(row.get(f"p{label}_pct")) for label in visible])
            vals.append(f"{_num(track.get('high_vs_auction_pp'))}/{_num(track.get('low_vs_auction_pp'))}")
        if stage == "open":
            vals.append(_num(track.get("latest_pct")))
        if stage == "close":
            vals.extend([_num(track.get("close_pct")), _num(track.get("delta_close_pp"))])
        state = row.get("auction_state") or row.get("auction_price_status") or "—"
        if stage == "close" and not track.get("close_pct") and track.get("close_pct") is None:
            state = (state + "；" if state != "—" else "") + "收盘缺失"
        vals.append(state)
        html_rows.append("<tr>" + "".join(f'<td>{_esc(value)}</td>' for value in vals) + "</tr>")
        text_rows.append(" | ".join(str(value) for value in vals))
    th = "".join(f'<th style="text-align:left;padding:4px 5px;white-space:nowrap">{_esc(value)}</th>' for value in headers)
    table = f'<table role="presentation" width="100%" cellspacing="0" cellpadding="4" style="border-collapse:collapse;font-size:11px;white-space:nowrap"><thead><tr>{th}</tr></thead><tbody>{"".join(html_rows)}</tbody></table>'
    title = {"auction": "竞价先选极值集合", "open": "竞价集合开盘追踪", "close": "竞价集合收盘追踪"}[stage]
    note = "固定集合由≤09:25竞价绝对涨幅前20与金额前20并集；金额阈值3000万元；当日描述。"
    if stage == "close":
        note += " 兑现/修复与回落/走弱榜仅在该集合内排序。"
    if stage == "open":
        note += f" 开盘追踪截至{checkpoint}及当前可得实时值。"
    legend = str(board.get("rank_legend") or "绝对值#=竞价涨跌幅绝对值排名；额#=竞价匹配额排名。")
    note += " " + legend
    blocks = [_section(title, table if rows else "<p>缺少符合门槛的竞价样本。</p>", note)]
    plain = [title + ("：\n" + "\n".join(text_rows) if text_rows else "：缺少符合门槛的竞价样本。"), legend]
    if omitted:
        note_line = f"另省略 {omitted} 只"
        blocks.append(f"<div>{note_line}</div>")
        plain.append(note_line)
    if stage != "auction":
        delta_key = "delta_latest_pp" if stage == "open" else "delta_close_pp"
        endpoint_label = "实时" if stage == "open" else "收盘"
        group_label = f"09:{checkpoint[-2:]}" if stage == "open" else "收盘"
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
    cross = chapters.get("plate_cross_table") if isinstance(chapters.get("plate_cross_table"), Mapping) else {}
    all_cross_rows = _rows(cross.get("rows"))
    broad_rows = [row for row in all_cross_rows if row.get("class") == "BROAD"]
    cross_rows = _rows(cross.get("main_rows"))[:10] or [row for row in all_cross_rows if row.get("class") != "BROAD"][:10]
    if cross_rows:
        checkpoint = str(cross.get("checkpoint") or ("0945" if stage == "open" else "0925"))
        headers = ("板块", "成员", "成员合计额", "金额覆盖", "份额", "自身历史倍数/分位/n", "等权涨幅", "涨/跌停", "最高板", "晋级率", "较昨日同截点", "龙头事实")
        html_rows, text_rows = [], []
        for row in cross_rows:
            amount = row.get("amount_yuan")
            share = row.get("market_share_pct")
            pct = row.get("equal_weight_pct")
            promotion = row.get("promotion_rate")
            promotion_status = row.get("promotion_status")
            yesterday = row.get("yesterday_same_checkpoint", {})
            yesterday_text = (f"{float(yesterday['change_pp']):+.2f}pp"
                              if isinstance(yesterday, Mapping) and isinstance(yesterday.get("change_pp"), (int, float))
                              else "UNKNOWN")
            leaders = row.get("leaders") if isinstance(row.get("leaders"), Mapping) else {}
            amount_coverage = row.get("amount_coverage") if isinstance(row.get("amount_coverage"), Mapping) else {}
            lead_bits = []
            for key, prefix in (("amount_max", "额"), ("highest_board", "板"), ("earliest_seal", "封")):
                lead = leaders.get(key)
                if isinstance(lead, Mapping):
                    suffix = f"{lead.get('value')}板" if key == "highest_board" else str(lead.get("seal_time") or "") if key == "earliest_seal" else ""
                    lead_bits.append(f"{prefix}:{lead.get('name') or lead.get('symbol')}{suffix}")
            if leaders.get("reason_sclt"):
                lead_bits.append("因:" + "/".join(map(str, leaders["reason_sclt"][:2])))
            hist_multiple = row.get("historical_amount_multiple")
            hist_percentile = row.get("historical_amount_percentile")
            hist_n = row.get("historical_amount_sample_days", 0)
            historical = (f"{float(hist_multiple):.2f}×/P{float(hist_percentile):.0f}/n={hist_n}"
                          if isinstance(hist_multiple, (int, float)) and isinstance(hist_percentile, (int, float))
                          else "样本少" if row.get("historical_amount_status") == "样本少"
                          else f"UNKNOWN/n={hist_n}")
            overlap = row.get("overlap_with") if isinstance(row.get("overlap_with"), list) else []
            overlap_text = ("；重叠≥0.8:" + "/".join(str(item.get("name") or item.get("code")) for item in overlap)
                            if overlap else "")
            promotion_text = (f"{row.get('promotion_numerator')}/{row.get('promotion_denominator')}({float(promotion):.0%})"
                              if promotion is not None and promotion_status == "READY" else
                              "样本少" if promotion_status == "样本少" else "未知")
            cells = [str(row.get("name") or row.get("code") or "—") + overlap_text, str(row.get("member_count", "—")),
                     "—" if amount is None else f"{float(amount) / 1e8:.2f}亿",
                     f"{amount_coverage.get('known', 0)}/{amount_coverage.get('members', 0)}",
                     "—" if share is None else f"{float(share):.2f}%",
                     historical,
                     "—" if pct is None else f"{float(pct):+.2f}%",
                     f"{row.get('limit_up_count', '—')}/{row.get('limit_down_count', '—')}",
                     "—" if row.get("highest_board") is None else f"{row['highest_board']}板",
                     promotion_text,
                     yesterday_text, "；".join(lead_bits) or "—"]
            html_rows.append("<tr>" + "".join(f"<td>{_esc(value)}</td>" for value in cells) + "</tr>")
            text_rows.append(" | ".join(cells))
        table = ("<div style=\"overflow-x:auto\"><table><thead><tr>" +
                 "".join(f"<th>{_esc(header)}</th>" for header in headers) +
                 "</tr></thead><tbody>" + "".join(html_rows) + "</tbody></table></div>")
        note = (f"{checkpoint} 同截点；金额=成员合计，重叠成员在不同板块重复计入；金额覆盖为有额成员/总成员；"
                "较昨日仅使用前一交易日同截点，缺失显示 UNKNOWN。龙头按金额最大/最高板/最早封板/原因SCLT分列。")
        broad_summary = ""
        broad_data = cross.get("broad_summary") if isinstance(cross.get("broad_summary"), Mapping) else {}
        broad_names = broad_data.get("names") if isinstance(broad_data.get("names"), list) else [row.get("name") for row in broad_rows]
        broad_count = broad_data.get("count", len(broad_rows))
        if broad_count:
            distinct_count = broad_data.get("distinct_stock_count")
            distinct_share = broad_data.get("distinct_market_share_pct")
            mean_return = broad_data.get("equal_weight_pct_mean")
            if isinstance(mean_return, (int, float)):
                broad_summary = (f"宽概念 {broad_count} 个，去重后覆盖股票 {distinct_count if distinct_count is not None else '未知'} 只，"
                                f"占全市场成交 {float(distinct_share):.2f}%" if isinstance(distinct_share, (int, float)) else
                                f"宽概念 {broad_count} 个，去重后覆盖股票 {distinct_count if distinct_count is not None else '未知'} 只，占全市场成交未知")
                broad_summary += (
                                f"；等权涨幅均值 {float(mean_return):+.2f}%：" + "、".join(map(str, broad_names)))
            else:
                broad_summary = (f"宽概念 {broad_count} 个，去重后覆盖股票 {distinct_count if distinct_count is not None else '未知'} 只，"
                                f"占全市场成交 {float(distinct_share):.2f}%" if isinstance(distinct_share, (int, float)) else
                                f"宽概念 {broad_count} 个，去重后覆盖股票 {distinct_count if distinct_count is not None else '未知'} 只，占全市场成交未知")
                broad_summary += (
                                "；等权涨幅未知：" + "、".join(map(str, broad_names)))
        html = _section(f"板块横向比较 @{checkpoint}", ("<p>" + _esc(broad_summary) + "</p>" if broad_summary else "") + table, note)
        text = "板块横向比较：\n" + (broad_summary + "\n" if broad_summary else "") + "\n".join(text_rows)
        if stage == "open":
            linkage = chapters.get("plate_linkage") if isinstance(chapters.get("plate_linkage"), Mapping) else {}
            changes = _rows(linkage.get("opening_changes"))[:8]
            if changes:
                change_headers = ("板块", "上涨占比变化(pp)", "等权中位涨幅变化(pp)", "涨停数变化", "跌停数变化")
                change_rows, change_text = [], []
                for row in changes:
                    delta = row.get("delta") if isinstance(row.get("delta"), Mapping) else {}
                    cells = [str(row.get("name") or row.get("code") or "—"),
                             "—" if delta.get("up_share_pct") is None else f"{float(delta['up_share_pct']):+.2f}",
                             "—" if delta.get("equal_weight_median_pct") is None else f"{float(delta['equal_weight_median_pct']):+.2f}",
                             str(delta.get("limit_up_count", "—")), str(delta.get("limit_down_count", "—"))]
                    change_rows.append("<tr>" + "".join(f"<td>{_esc(value)}</td>" for value in cells) + "</tr>")
                    change_text.append(" | ".join(cells))
                change_table = ("<div style=\"overflow-x:auto\"><table><thead><tr>" +
                                "".join(f"<th>{_esc(header)}</th>" for header in change_headers) +
                                "</tr></thead><tbody>" + "".join(change_rows) + "</tbody></table></div>")
                html += _section(f"0925→{checkpoint} 盘中变化", change_table,
                                 f"当日 09:25 至 {checkpoint} 的变化窗口；与较昨日同截点分开。")
                text += f"\n0925→{checkpoint} 盘中变化：\n" + "\n".join(change_text)
        return html, text
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


def _a_to_b(report: Mapping[str, Any], stage: str, limit: int | None = None) -> tuple[str, str]:
    entries = _rows(report.get("a_to_b"))
    if not isinstance(report.get("a_to_b"), list):
        msg = "数据缺口：未生成看A做B"
        return _section("看A做B", f'<div>{msg}</div>'), msg
    if not entries:
        msg = "看A做B：无合格A"
        return _section("看A做B", f'<div>{msg}</div>'), msg
    if any(isinstance(entry, Mapping) and entry.get("schema_version") == 2 for entry in entries):
        return _a_to_b_v2(report, entries, stage, limit)
    output, plain = [], []
    displayed = entries[:8] if limit is None else entries[:min(8, limit)]
    for entry in displayed:
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
    if limit is not None and len(entries[:8]) > len(displayed):
        note = f"另省略 {len(entries[:8]) - len(displayed)} 条"
        output.append(f"<div>{note}</div>")
        plain.append(note)
    return _section("看A做B", "".join(output), "点时同板块候选；不构成建议"), "看A做B：\n" + "\n".join(plain)


def _a_to_b_v2(report: Mapping[str, Any], entries: Sequence[Mapping[str, Any]], stage: str, limit: int | None = None) -> tuple[str, str]:
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
    displayed = entries[:limit] if limit is not None else entries
    for entry in displayed:
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
        track_keys = (["0925"] if stage == "auction" else [
            label for label, _ in OPEN_CHECKPOINTS if label <= _open_checkpoint_label(report)])
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
            + f"；等权涨幅 {pct(plate.get('equal_weight_pct'))}→{pct(plate.get('current_equal_weight_pct'))}"
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
                    cp = _open_checkpoint_label(report)
                    line += f" | {cp}相对板块 {pp(bm.get(f'relative_to_plate_{cp}_pp'))}"
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
    if len(displayed) < len(entries):
        note = f"另省略 {len(entries) - len(displayed)} 条"
        html_blocks.append(f"<div>{note}</div>")
        text_blocks.append(note)
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


def _front_page_inputs(report: Mapping[str, Any], stage: str) -> Mapping[str, Any]:
    data = report.get("front_page")
    if isinstance(data, Mapping):
        return data
    metadata: dict[str, dict[str, Any]] = {}
    from .ladder_cluster import build_front_page_data
    stocks = report.get("stocks") if isinstance(report.get("stocks"), list) else []
    return build_front_page_data(report, stage=stage, plate_metadata=metadata, stocks=stocks)


def _front_page_view(report: Mapping[str, Any], stage: str) -> tuple[str, str]:
    data = _front_page_inputs(report, stage)
    chapters = report.get("chapters") if isinstance(report.get("chapters"), Mapping) else {}
    market = chapters.get("market_price_volume") if isinstance(chapters.get("market_price_volume"), Mapping) else {}
    terminal = report.get("auction_terminal_0925") if isinstance(report.get("auction_terminal_0925"), Mapping) else {}
    if stage == "auction":
        current = terminal or market
    else:
        current = market
    amount = _first(current, "market_match_amount_yuan", "match_amount_yuan")
    median = _first(current, "median_pct_change", "median_open_pct")
    up_share = current.get("up_share_pct")
    if up_share is None and isinstance(current.get("up_share"), (int, float)):
        up_share = current["up_share"] * 100
    baseline = current.get("auction_baseline_comparison") if isinstance(current.get("auction_baseline_comparison"), Mapping) else {}
    multiple, percentile = baseline.get("multiple"), baseline.get("percentile")
    previous = current.get("vs_previous_day_final")
    yesterday = chapters.get("yesterday_highboards_summary") if isinstance(chapters.get("yesterday_highboards_summary"), Mapping) else {}
    pool = yesterday.get("yesterday_limit_pool") if isinstance(yesterday.get("yesterday_limit_pool"), Mapping) else {}
    carry = pool.get("mean_pct_change")
    amount_text = ("开盘缺失" if stage == "open" else "竞价缺失") if amount is None else (
        f"开盘{float(amount) / 1e8:.1f}亿" if stage == "open" else f"竞价{float(amount) / 1e8:.1f}亿")
    amount_comparison = (f"昨{float(previous):.2f}倍" if isinstance(previous, (int, float)) else
                         f"64日{float(multiple):.2f}倍/P{float(percentile):.0f}" if isinstance(multiple, (int, float)) and isinstance(percentile, (int, float)) else "倍数/分位缺失")
    if stage == "auction":
        amount_short = "竞价缺失" if amount is None else f"竞价{float(amount) / 1e8:.0f}亿"
        headline = f"{amount_short}｜{amount_comparison.replace('倍/P', '倍P')}｜中{_num(median)}｜涨{_num(up_share)}｜昨板{_num(carry)}"
    else:
        amount_short = "开盘缺失" if amount is None else f"开盘{float(amount) / 1e8:.0f}亿"
        headline = f"{amount_short}｜{amount_comparison}｜中{_num(median)}｜涨{_num(up_share)}｜昨板{_num(carry)}"
    cutoff_label = _open_checkpoint_label(report) if stage == "open" else "0925"
    lines = [f"{cutoff_label}｜{headline}", "【昨高标·题材聚类】"]
    feedback = report.get("streak_feedback") if isinstance(report.get("streak_feedback"), Mapping) else {}
    feedback_point = feedback.get("checkpoints", {}).get(cutoff_label, {}) if isinstance(feedback, Mapping) else {}
    feedback_matrix = feedback_point.get("matrix", {}) if isinstance(feedback_point, Mapping) else {}
    feedback_levels = feedback_matrix.get("by_level", {}) if isinstance(feedback_matrix, Mapping) else {}
    if feedback_levels:
        rates = [f"{level}晋级{stats.get('promoted', 0)}/{stats.get('total', 0)}({stats.get('promotion_rate'):.0%})"
                 for level, stats in feedback_levels.items() if isinstance(stats.get("promotion_rate"), (int, float))]
        feedback_overall = feedback_matrix.get("overall", {})
        if rates:
            themes = feedback_matrix.get("by_theme", {})
            theme_rates = [f"{stats.get('theme', key)} {stats.get('promoted', 0)}/{stats.get('total', 0)}"
                           f"({stats.get('promotion_rate'):.0%},未知{stats.get('unknown', 0)})"
                           for key, stats in sorted(themes.items(), key=lambda item: (-item[1].get("total", 0), item[0]))[:3]
                           if isinstance(stats.get("promotion_rate"), (int, float))]
            lines.insert(1, "【昨日连板今日表现】" + "、".join(rates[:5]) +
                         ("；主题 " + "、".join(theme_rates) if theme_rates else "") +
                         ("；竞价暂时" if feedback_point.get("provisional") else "") +
                         (f"；均值{feedback_overall['mean_return_pct']:+.2f}%/中位{feedback_overall['median_return_pct']:+.2f}%/红盘{feedback_overall['red_share']:.0%}"
                          if isinstance(feedback_overall.get("mean_return_pct"), (int, float))
                          and isinstance(feedback_overall.get("median_return_pct"), (int, float))
                          and isinstance(feedback_overall.get("red_share"), (int, float)) else "；涨幅统计 UNKNOWN"))
    ladder = data.get("ladder") if isinstance(data.get("ladder"), Mapping) else {}
    clusters = ladder.get("clusters", []) if isinstance(ladder.get("clusters"), list) else []
    if not clusters:
        lines.append(str(ladder.get("empty_message") or "无昨高标簇：数据缺失"))
    visible_clusters = clusters
    for cluster in visible_clusters:
        share = cluster.get("share_bp")
        delta = cluster.get("delta_share_bp")
        share_text = "份额—" if share is None else f"额占比{float(share):.0f}bp"
        if delta is not None and float(delta) != 0:
            share_text += f"({float(delta):+.0f})"
        eq = _num(cluster.get("equal_weight_pct"))
        judgement = cluster.get("judgement") or "—"
        if stage == "open":
            trend = cluster.get("judgement_trend")
            if trend and trend not in {"信息缺失", "较竞价分化"}:
                judgement += f"·较竞价{trend}"
        lead_text = ""
        if stage == "open":
            path_tags = [label for label, _ in OPEN_CHECKPOINTS
                         if label <= cutoff_label]
            paths = cluster.get("paths", [])
            row = paths[0] if paths else {}
            members = (f"高标{row.get('name')}昨{int(row.get('streak') or 0)}板 " +
                       "→".join(_num(row.get(tag)) for tag in path_tags)
                       if row else "路径缺失")
            for row in paths[:1]:
                auction_pct, open_pct = row.get("0925"), row.get(cutoff_label)
                symbol = str(row.get("symbol") or "")
                limit = 20.0 if symbol.startswith(("300", "301", "688", "689")) else 30.0 if symbol.startswith(("4", "8", "92")) else 10.0
                if auction_pct is not None and auction_pct >= limit - 0.2 and open_pct is not None and open_pct < limit - 0.2:
                    path_status = "炸板价代理"
                elif auction_pct is not None and open_pct is not None and open_pct > auction_pct + 0.2:
                    path_status = "转强"
                elif auction_pct is not None and open_pct is not None and open_pct < auction_pct - 0.2:
                    path_status = "回落"
                else:
                    path_status = "保持" if auction_pct is not None and open_pct is not None else "路径缺失"
                row["path_status"] = path_status
                members += f" {path_status}"
            lead_text = members
        else:
            paths = cluster.get("paths", [])
            row = paths[0] if paths else {}
            lead_text = (f"{row.get('name')}{int(row.get('streak') or 0)}板"
                         f"{_num(row.get('0925'))}{row.get('status') or '状态缺失'}" if row else "反馈缺失")
        hot_note = "·今日热榜" if cluster.get("hot_today") else ""
        delta_text = "" if delta is None else f" Δ{float(delta):+.0f}bp"
        leader = cluster.get("leader", {}) if isinstance(cluster.get("leader"), Mapping) else {}
        leader_name = _short(leader.get("name") or (cluster.get("paths") or [{}])[0].get("name") or "高标", 5)
        paths = cluster.get("paths", []) if isinstance(cluster.get("paths"), list) else []
        members_text = "、".join(f"{row.get('name') or '名称未知'}昨{int(row.get('streak') or 0)}板"
                                 for row in paths)
        amount = cluster.get("amount_yuan")
        amount_text = "簇额—" if amount is None else f"簇额{float(amount) / 1e8:.2f}亿"
        lines.append(f"{_short(cluster.get('name'), 8)}{hot_note}{delta_text} {members_text or '成员—'} "
                     f"{amount_text}/{share_text} 等权{eq} {leader_name}{judgement}")
    lines.append(str(ladder.get("first_board_summary") or "昨首板反馈缺失"))
    lines.append(f"【板块强弱@{cutoff_label}｜市场额同截点】")
    strength = data.get("plate_strength") if isinstance(data.get("plate_strength"), Mapping) else {}
    for label, key in (("变强", "up"), ("变弱", "down")):
        rows = strength.get(key, []) if isinstance(strength.get(key), list) else []
        if not rows:
            lines.append(f"{label}：{strength.get('message') or '无显著变化'}")
        for row in rows[:2]:
            delta = float(row.get("delta_bp", 0))
            pct = row.get("equal_weight_pct")
            leader = row.get("top_stock_name") or "龙头未知"
            leader_pct = _num(row.get("top_stock_pct")) if row.get("top_stock_pct") is not None else "涨幅缺失"
            share_value = row.get("share_bp")
            share_display = "—" if share_value is None else f"{float(share_value):.0f}bp"
            lines.append(f"{label} {_short(row.get('name'), 8)} Δ{delta:+.0f}bp 等权{_num(pct)} {_short(leader, 5)}{leader_pct}")
        if len(rows) > 2:
            lines.append(f"{label}另{len(rows)-2}个见附表")
    story = report.get("story") if isinstance(report.get("story"), Mapping) else {}
    events = story.get("top_events", []) if isinstance(story.get("top_events"), list) else []
    lines.append(f"【今日{min(3, len(events))}件不一样】")
    for event in events[:3]:
        summary = str(event.get("summary") or "") if isinstance(event, Mapping) else ""
        event_id = event.get("id") if isinstance(event, Mapping) else None
        summary = summary.replace("；板块份额变化最大：中报增长 +0bp", "；板块份额变化未达显著阈值")
        if event_id == "auction_amount_context" and stage == "auction":
            baseline = current.get("auction_baseline_comparison") if isinstance(current.get("auction_baseline_comparison"), Mapping) else {}
            lines.append(f"竞价额{float(amount) / 1e8:.1f}亿，64日{float(baseline.get('multiple')):.2f}倍/P{float(baseline.get('percentile')):.0f}"
                         if isinstance(amount, (int, float)) and isinstance(baseline.get("multiple"), (int, float)) and isinstance(baseline.get("percentile"), (int, float))
                         else _short(summary.rstrip("。"), 40))
        elif event_id == "auction_breadth_context" and stage == "auction":
            compact = (summary.replace("昨日收盘中位", "昨中位").replace("今日竞价", "竞价")
                       .replace("上涨占比近", "涨占比近").replace("由高到低第", "第"))
            lines.append(_short(compact.rstrip("。"), 40))
        elif event_id == "auction_ladder_plate_context":
            lines.append(summary.replace("+0bp", "变化未显著").rstrip("。"))
        else:
            lines.append(summary.rstrip("。") if summary else "信息缺失")
    funnel = story.get("funnel") if isinstance(story.get("funnel"), Mapping) else {}
    lines.append("【能做 / 要等 / 别碰】")
    for label, key in (("能做", "watch"), ("要等", "wait"), ("别碰", "exclude")):
        rows = funnel.get(key, []) if isinstance(funnel.get(key), list) else []
        counts = funnel.get("counts", {}) if isinstance(funnel.get("counts"), Mapping) else {}
        try:
            total_count = max(0, int(counts.get(key, len(rows))))
        except (TypeError, ValueError):
            total_count = len(rows)
        visible = min(2, total_count, len(rows))
        lines.append(f"{label}({visible})")
        if not visible:
            lines.append(f"{label}：无")
        for row in rows[:visible]:
            condition = row.get("condition")
            if isinstance(condition, Mapping):
                condition = condition.get("text")
            condition_text = str(condition or "")
            risk_text = str(row.get("risk") or "")
            lines.append(_short(f"{_display_name(row, 5)} {condition_text}；险{risk_text}", 40))
        overflow = max(0, total_count - visible)
        if overflow:
            lines.append(f"另{overflow}只")
    lines.append("归属来源：" + str(ladder.get("attribution_basis") or "登记表回退"))
    basis = str(report.get("front_page", {}).get("basis") or "") if isinstance(report.get("front_page"), Mapping) else ""
    if basis and basis not in {"时点未知", "RELATION_ASOF_UNKNOWN", "UNKNOWN"}:
        lines.append("关系" + basis)
    lines.append("口径：封停价代理；缺失不补零")
    lines = [_short(line, 40) for line in lines]
    if stage == "open":
        lines = [line.replace("收盘", "收市").replace("close_pct", "").replace("close_mean", "").replace("close_median", "") for line in lines]
    html_lines = "".join(f"<div style=\"padding:4px 6px;border-bottom:1px solid #e5eaf0\">{_esc(line)}</div>" for line in lines)
    markup = f'<section style="margin:8px 0;padding:10px;background:#fff;border:1px solid #dbe3ec;border-radius:8px">{html_lines}</section>'
    return markup, "\n".join(lines)


def _render_open_verification_board(report: Mapping[str, Any], trade_date: str) -> tuple[str, str]:
    def mapping(value):
        return value if isinstance(value, Mapping) else {}
    def number(value):
        try:
            n = float(value)
            return n if math.isfinite(n) else None
        except (TypeError, ValueError):
            return None
    def rank(row, key):
        value = number(row.get(key))
        return value if value is not None else float('-inf')
    def pp(value):
        value = number(value)
        return f'{value:+.2f}pp' if value is not None else '—'
    def pct(value):
        return _num(value, 2)
    def table(headers, rows, marker):
        head = ''.join(f'<th style="text-align:left;padding:8px;background:#edf3fa">{_esc(h)}</th>' for h in headers)
        body = ''.join(f'<tr data-item="{marker}">' + ''.join(f'<td style="padding:8px;border-bottom:1px solid #e8edf4">{v}</td>' for v in row) + '</tr>' for row in rows)
        return f'<table width="100%" cellspacing="0" style="font-size:14px;border-collapse:collapse"><tr>{head}</tr>{body}</table>' if rows else '<div>数据缺口：未生成</div>'
    chapters = mapping(report.get('chapters'))
    market = mapping(chapters.get('market_price_volume'))
    cross = mapping(chapters.get('plate_cross_table'))
    plates = _rows(cross.get('main_rows')) or _rows(cross.get('rows'))
    plates = [r for r in plates if r.get('class') != 'BROAD' and not any(w in str(r.get('name') or '') for w in ('季报', '年报', '中报'))]
    plates.sort(key=lambda r: rank(r, 'amount_yuan'), reverse=True)
    up, down = _rows(chapters.get('sealed_up')), _rows(chapters.get('sealed_down'))
    highs = sorted(_rows(chapters.get('yesterday_highboards')), key=lambda r: rank(r, 'streak'), reverse=True)
    extremes = mapping(mapping(chapters.get('extremes_board')).get('open'))
    top = mapping(extremes.get('amount_top20'))
    sealed = [r for r in up if r.get('status') == '封死']
    opened = [r for r in up if r.get('status') in ('开板', '炸板')]
    pressure = number(market.get('down_amount_share_pct'))
    pressure_text = pct(pressure) + (' 高抛压/高低切换' if pressure is not None and pressure > 65 else '')
    def count(rows, source):
        return str(len(rows)) if isinstance(chapters.get(source), list) else '—'
    cards = [('成交额', _amount_with_unit(market.get('market_match_amount_yuan')), '#d44c4c'),
             ('下跌额占比（抛压）', pressure_text, '#d44c4c' if pressure is not None and pressure > 65 else '#53677e'),
             ('额Top20红盘', f'{top.get("red", "—")}/{top.get("n", "—")} · 加权均涨 {pct(top.get("amount_weighted_pct"))}', '#e69a38'),
             ('一字封死 / 开板 / 跌停', f'<span style="color:#bc3541">{count(sealed, "sealed_up")}</span> / <span style="color:#bd6a20">{count(opened, "sealed_up")}</span> / <span style="color:#16875b">{count(down, "sealed_down")}</span>', '#35a77b')]
    card_html = ''.join(f'<td width="25%" style="vertical-align:top;padding:9px;background:white;border-top:4px solid {color}"><span>{_esc(title)}</span><br><b style="color:{color}">{value}</b></td>' for title, value, color in cards)
    quadrant = mapping(chapters.get('plate_quadrants'))
    quadrant_rows = _rows(quadrant.get('rows')) if quadrant.get('available') is not False else []
    def eligible(row):
        return 'ST' not in str(row.get('name') or '').upper() and '泛化名单' not in (row.get('flags') or [])
    def quadrant_pct(row):
        value = number(row.get('kaipan_pct'))
        return value if value is not None else number(row.get('our_pct'))
    if quadrant_rows:
        attack = [r for r in quadrant_rows if eligible(r) and quadrant_pct(r) is not None and quadrant_pct(r) > 0]
        attack.sort(key=lambda r: rank(r, 'strength'), reverse=True)
        flow = [[_esc(r.get('name') or '—'), _esc(r.get('strength') if number(r.get('strength')) is not None else '—'), pct(quadrant_pct(r)), _amount_with_unit(r.get('amount_yuan'))] for r in attack[:5]]
    else:
        attack = [r for r in plates if eligible(r) and ((number(r.get('equal_weight_pct')) is not None and number(r.get('equal_weight_pct')) > 0) or (number(r.get('limit_up_count')) is not None and number(r.get('limit_up_count')) >= 2))]
        flow = [[_esc(r.get('name') or '—'), '—', pct(r.get('equal_weight_pct')), _amount_with_unit(r.get('amount_yuan'))] for r in attack[:5]]
    bleeding = [r for r in plates if number(r.get('equal_weight_pct')) is not None and number(r.get('equal_weight_pct')) <= -1.0]
    if any(r.get('name') == 'CPO/MPO' for r in bleeding):
        bleeding = [r for r in bleeding if r.get('name') != '光模块']
    bleeding_pills = ' ｜ '.join(f'{_esc(r.get("name") or "—")} {_amount_with_unit(r.get("amount_yuan"))} {pct(r.get("equal_weight_pct"))}' for r in bleeding[:3]) or '数据缺口：未生成'
    changes = mapping(chapters.get('plate_changes'))
    retreat = sorted(_rows(changes.get('down')), key=lambda r: number(r.get('change_pp')) if number(r.get('change_pp')) is not None else float('inf'))[:2]
    pills = ' '.join(f'<span style="display:inline-block;background:#eaf8ef;padding:4px 8px;border-radius:12px">{_esc(r.get("name") or "—")} {pp(r.get("change_pp"))}</span>' for r in retreat) or '数据缺口：未生成'
    checkpoint = _open_checkpoint_label(report)
    def current(row):
        return _first(row, 'current_pct', checkpoint)
    emerging = sorted(_rows(chapters.get('emerging')), key=lambda r: rank(r, 'match_amount_yuan'), reverse=True)[:5]
    erows = []
    for r in emerging:
        a, b = number(r.get('0925')), number(current(r))
        erows.append([_esc(r.get('name') or '—'), f'{pct(a)} → {pct(b)}', f'{_amount_with_unit(r.get("match_amount_yuan"))} · {pp(b-a if a is not None and b is not None else None)}'])
    tracked = [r for r in _rows(extremes.get('rows')) if number(mapping(r.get('tracking')).get('delta_latest_pp')) is not None and number(mapping(r.get('tracking')).get('delta_latest_pp')) < 0]
    tracked.sort(key=lambda r: number(mapping(r.get('tracking')).get('delta_latest_pp')))
    trows = [[f'{_esc(r.get("name") or "—")} ({_esc(r.get("main_plate") or "—")})', f'{pct(r.get("auction_pct"))} → {pct(mapping(r.get("tracking")).get("latest_pct"))}', pp(mapping(r.get('tracking')).get('delta_latest_pp'))] for r in tracked[:3]]
    def block(row, color, bg, text, marker):
        return f'<span data-item="{marker}" style="display:inline-block;margin:3px;padding:7px;background:{bg};border-left:3px solid {color};color:{color}">{_esc(text)}</span>'
    # Limit the combined stream while retaining its specified priority.
    block_rows = [(r, color, bg, label) for rows, color, bg, label in ((sealed, '#bb3541', '#fff0f0', '封死'), (opened, '#bd6a20', '#fff4e5', '开板'), (down, '#16875b', '#eaf8ef', '跌停')) for r in rows if 'ST' not in str(r.get('name') or '').upper()][:12]
    def seal_text(row, label):
        amount = number(row.get('seal_yuan'))
        if label == '封死' and amount is not None and amount > 0:
            label = f'封{amount / 1e8:.1f}亿' if amount >= 1e8 else f'封{amount / 1e4:.0f}万'
        return f'{row.get("name") or "—"} · {label}'
    blocks = ''.join(block(r, color, bg, seal_text(r, label), 'open-seal') for r, color, bg, label in block_rows) or '数据缺口：未生成'
    high_rows = [r for r in highs if number(r.get('streak')) is not None and number(r.get('streak')) >= 3]
    high_rows = high_rows[:6] if len(high_rows) >= 4 else highs[:6]
    high_blocks = []
    for r in high_rows:
        value = number(current(r)); status = str(r.get('status') or '未知')
        if status == '封死': color, bg = '#bb3541', '#fff0f0'
        elif status in ('开板', '炸板'): color, bg = '#bd6a20', '#fff4e5'; status = '开板'
        elif value is not None and value < 0: color, bg = '#16875b', '#eaf8ef'; status = '水下'
        else: color, bg = '#5c6d80', '#f2f5f8'
        high_blocks.append(block(r, color, bg, f'{r.get("name") or "—"} · 昨{r.get("streak") if r.get("streak") is not None else "—"}板 · {pct(value)} · {status}', 'open-highboard'))
    sections = (f'<header style="background:#142c4a;color:white;padding:15px"><b style="font-size:20px">开盘快报 · 钱去了哪</b><br>{_esc(trade_date)} · {checkpoint}｜竞价 → 当前</header>'
        f'<table width="100%" cellspacing="4"><tr>{card_html}</tr></table>'
        f'<section data-section="open-money-flow"><h2>① 钱去了哪：🔥 真进攻主线 Top 5</h2><div>成员成交额可能重叠，不可相加</div>{table(["进攻主线", "强度", "涨幅", "成交额"], flow, "open-flow-row")}<div style="padding:8px;background:#eaf8ef;color:#16875b;border-radius:12px">🩸 放量失血：{bleeding_pills}<br>💧 撤退/回落最快 {pills}</div></section>'
        f'<section data-section="open-verification"><h2>② 确定性异动与风险</h2><h3>🔒 硬封先锋 &amp; 开板/跌停警示</h3>{blocks}<h3>🚀 大资金真承接 / 急拉 Top 5</h3>{table(["股票", "09:25 → 当前", "成交额·Δ"], erows, "open-emerging-row")}<h3>⚠️ 砸盘/炸板避坑 Top 3</h3>{table(["股票(题材)", "09:25 → 当前", "Δ回撤"], trows, "open-retreat-row")}</section>'
        f'<section><h2>③ 核心高标状态</h2>{"".join(high_blocks) or "数据缺口：未生成"}</section>')
    plain = html.unescape(re.sub(r'<[^>]+>', ' ', sections))
    return sections, re.sub(r'\s+', ' ', plain).strip()


def _render_close_v3_board(report: Mapping[str, Any], trade_date: str) -> tuple[str, str]:
    def mapping(value):
        return value if isinstance(value, Mapping) else {}
    def number(value):
        try:
            n = float(value)
            return n if math.isfinite(n) else None
        except (TypeError, ValueError):
            return None
    def pct(value):
        return _num(value, 2)
    def pp(value):
        value = number(value)
        return f'{value:+.2f}pp' if value is not None else '—'
    def table(headers, rows, marker):
        head = ''.join(f'<th style="text-align:left;padding:8px;background:#edf3fa">{_esc(h)}</th>' for h in headers)
        body = ''.join(f'<tr data-item="{marker}">' + ''.join(f'<td style="padding:8px;border-bottom:1px solid #e8edf4">{v}</td>' for v in row) + '</tr>' for row in rows)
        return f'<table width="100%" cellspacing="0" style="font-size:14px;border-collapse:collapse"><tr>{head}</tr>{body}</table>' if rows else '<div>数据缺口：未生成</div>'
    def chip(color, bg, text, marker):
        return f'<span data-item="{marker}" style="display:inline-block;margin:3px;padding:7px 9px;background:{bg};border-left:3px solid {color};color:{color};font-size:13px">{_esc(text)}</span>'

    market = mapping(report.get('market'))
    regime = str(market.get('regime_label') or '盘面复盘')
    auction_med = pct(market.get('auction_median'))
    close_med = pct(_first(market, 'close_median', 'close_median_pct'))
    amt_text = _amount_with_unit(market.get('amount_yuan'))
    auc_amt = _amount_with_unit(market.get('auction_amount_yuan'))
    up_cnt = market.get('up_count')
    down_cnt = market.get('down_count')
    up_down_html = (f'<span style="color:#bc3541">{int(up_cnt)}涨</span> / <span style="color:#16875b">{int(down_cnt)}跌</span>'
                    if isinstance(up_cnt, (int, float)) and isinstance(down_cnt, (int, float)) else '—')
    eb_close = mapping(mapping(report.get('extremes_board')).get('close'))
    top20 = mapping(eb_close.get('amount_top20'))
    top20_text = (f'{top20.get("red", "—")}/{top20.get("n", "—")} · 均涨 {pct(top20.get("amount_weighted_pct"))}'
                  if top20 else '—')
    lu = market.get('close_limit_up_count')
    ld = market.get('close_limit_down_count')
    lu_ld_html = (f'<span style="color:#bc3541">{int(lu)}</span> / <span style="color:#16875b">{int(ld)}</span>'
                  if isinstance(lu, (int, float)) and isinstance(ld, (int, float)) else '—')

    yp_closure = mapping(report.get('yest_pool_closure'))
    if yp_closure and yp_closure.get('count'):
        yp_auc = pct(yp_closure.get('auction_mean_pct'))
        yp_cls = pct(yp_closure.get('close_mean_pct'))
        yp_prom = yp_closure.get('promoted_count', '—')
        yp_cnt = yp_closure.get('count', '—')
        card3_title = '昨涨停接力闭环'
        card3_val = (f'{yp_auc} → {yp_cls}<br>'
                     f'<span style="font-size:11px;color:#667085;font-weight:400">晋级 {yp_prom}/{yp_cnt}</span>')
        yp_c_num = number(yp_closure.get('close_mean_pct'))
        card3_color = '#bc3541' if yp_c_num is not None and yp_c_num > 0 else '#16875b' if yp_c_num is not None and yp_c_num < 0 else '#e69a38'
    else:
        card3_title = '额Top20红盘'
        card3_val = top20_text
        card3_color = '#e69a38'

    cards = [
        ('全天成交额', f'{amt_text}<br><span style="font-size:11px;color:#667085;font-weight:400">竞价 {auc_amt}</span>', '#d44c4c'),
        ('竞价 → 收盘中位', f'{auction_med} → {close_med}<br><span style="font-size:11px;color:#667085;font-weight:400">{up_down_html}</span>', '#53677e'),
        (card3_title, card3_val, card3_color),
        ('涨停 / 跌停（价代）', lu_ld_html, '#35a77b'),
    ]
    card_html = ''.join(
        f'<td width="25%" style="vertical-align:top;padding:9px;background:white;border-top:4px solid {color}">'
        f'<span style="font-size:12px;color:#53677e">{_esc(title)}</span><br><b style="font-size:14px;color:{color}">{value}</b></td>'
        for title, value, color in cards
    )

    plates_obj = mapping(report.get('plates'))
    bucket_closure = _rows(report.get('auction_bucket_closure'))
    first_boards = _rows(report.get('today_first_boards'))
    bottom_plates = [r for r in _rows(plates_obj.get('close_bottom10'))
                     if 'ST' not in str(r.get('name') or '').upper()][:4]
    bleeding_pills = ' ｜ '.join(
        f'{_esc(r.get("name") or "—")} {pct(r.get("close_mean"))}' for r in bottom_plates
    ) or '暂无重灾板块数据'

    if bucket_closure:
        verdict_styles = {
            '兑现加强': ('#bb3541', '#fff0f0', '🔥'),
            '弱转强修复': ('#1d4ed8', '#eff6ff', '🔄'),
            '高开回落': ('#bd6a20', '#fff4e5', '⚠️'),
            '持续失血': ('#16875b', '#eaf8ef', '🩸'),
            '震荡延续': ('#53677e', '#f2f5f8', '➖'),
        }
        c_rows = []
        for b in bucket_closure[:10]:
            rk = b.get('rank')
            rk_tag = f'#{rk} ' if rk is not None else ''
            a_p = b.get('auction_weighted_pct') if b.get('auction_weighted_pct') is not None else b.get('auction_mean_pct')
            c_p = b.get('close_weighted_pct') if b.get('close_weighted_pct') is not None else b.get('close_mean_pct')
            c_num = number(c_p)
            c_col = '#bc3541' if c_num is not None and c_num > 0 else '#16875b' if c_num is not None and c_num < 0 else '#53677e'
            d_pp = pp(b.get('delta_pp'))
            lu_c = b.get('close_limit_up_count')
            ld_c = b.get('close_limit_down_count')
            lu_ld_cell = (f'<span style="color:#bc3541">{int(lu_c)}</span>/<span style="color:#16875b">{int(ld_c)}</span>'
                          if isinstance(lu_c, (int, float)) and isinstance(ld_c, (int, float)) else '—')
            v = str(b.get('verdict') or '震荡延续')
            vc, vbg, vicon = verdict_styles.get(v, ('#53677e', '#f2f5f8', '➖'))
            top1_n = str(b.get('top1_name') or '').strip()
            top1_str = f'{_esc(top1_n)} {pct(b.get("top1_close_pct"))}' if top1_n else ''
            badge = f'<span style="display:inline-block;padding:2px 6px;border-radius:4px;background:{vbg};color:{vc};font-size:12px;font-weight:700">{vicon} {_esc(v)}</span>'
            c_rows.append([
                f'<b>{_esc(rk_tag + str(b.get("name") or "—"))}</b>',
                _amount_with_unit(b.get('auction_amount_yuan')),
                f'{pct(a_p)} → <b style="color:{c_col}">{pct(c_p)}</b> <span style="font-size:11px;color:#667085">({d_pp})</span>',
                lu_ld_cell,
                f'{badge}' + (f'<br><span style="font-size:11px;color:#53677e">{top1_str}</span>' if top1_str else ''),
            ])
        plate_section_title = '① 竞价分桶 → 收盘闭环验收（09:26 Top10）'
        plate_table_html = table(['竞价分桶', '竞价额', '竞价 → 收盘(Δ)', '涨/跌停', '闭环定性·龙头'], c_rows, 'close-plate-row')
        # Highlight intraday emerging 1st-board themes outside auction Top 10
        fb_counts: dict[str, int] = {}
        for fb in first_boards:
            th = str(fb.get('main_plate') or '').strip()
            if th and th != '未归类':
                fb_counts[th] = fb_counts.get(th, 0) + 1
        top_fb = sorted(fb_counts.items(), key=lambda x: (-x[1], x[0]))[:5]
        emerging_fb_html = (
            '<div style="margin-top:8px;padding:7px 10px;background:#fff5f5;color:#bb3541;border-radius:10px;font-size:13px">'
            '🌱 盘中新首板助攻：' + ' ｜ '.join(f'{_esc(k)} {v}首板' for k, v in top_fb) + '</div>'
        ) if top_fb else ''
        plate_footer_html = (
            emerging_fb_html
            + f'<div style="margin-top:6px;padding:7px 10px;background:#eaf8ef;color:#16875b;border-radius:10px;font-size:13px">🩸 重灾失血：{bleeding_pills}</div>'
        )
    else:
        top_plates = [r for r in (_rows(plates_obj.get('close_top10')) or _rows(plates_obj.get('auction_amount_top10')))
                      if 'ST' not in str(r.get('name') or '').upper()][:5]
        plate_rows = [
            [_esc(r.get('name') or '—'),
             str(int(r['members'])) if isinstance(r.get('members'), (int, float)) else '—',
             f'{pct(r.get("p0925_mean"))} → <b style="color:#bc3541">{pct(r.get("close_mean"))}</b>']
            for r in top_plates
        ]
        plate_section_title = '① 今日主线定调：🔥 抗跌/收强 Top 5'
        plate_table_html = table(['进攻/抗跌主线', '成员', '竞价 → 收盘'], plate_rows, 'close-plate-row')
        plate_footer_html = (
            f'<div style="margin-top:8px;padding:8px 10px;background:#eaf8ef;color:#16875b;border-radius:10px;font-size:13px">'
            f'🩸 重灾失血：{bleeding_pills}</div>'
        )

    unlimited = [r for r in (_rows(report.get('unlimited_price_band_stocks')) or _rows(report.get('stocks')))
                 if r.get('is_new_listing') or r.get('is_unlimited_price_band') or (r.get('listing_days') == 1)
                 or r in _rows(report.get('unlimited_price_band_stocks'))]
    unlimited_names = {r.get('name') for r in unlimited if r.get('name')}
    rebound = [r for r in (_rows(eb_close.get('rebound_top8')) or _rows(report.get('big_meat')))
               if not r.get('is_new_listing') and not r.get('is_unlimited_price_band')
               and r.get('name') not in unlimited_names][:5]
    rebound_rows = []
    for r in rebound:
        a = number(_first(r, 'auction_pct', 'p0925_pct'))
        c = number(r.get('close_pct'))
        delta = number(mapping(r.get('tracking')).get('delta_close_pp'))
        if delta is None and a is not None and c is not None:
            delta = c - a
        rebound_rows.append([
            _esc(_display_name(r, 10)),
            f'{pct(a)} → <b style="color:#bc3541">{pct(c)}</b>',
            f'<span style="color:#bc3541">{pp(delta)}</span>',
        ])
    unlimited_note = (' ｜ '.join(f'{_esc(_display_name(r, 10))} {pct(r.get("close_pct"))}' for r in unlimited[:5])
                      if unlimited else '')

    weak = _rows(eb_close.get('weak_top8'))[:5] or _rows(report.get('big_loss'))[:5]
    weak_rows = []
    for r in weak:
        a = number(_first(r, 'auction_pct', 'p0925_pct'))
        c = number(r.get('close_pct'))
        delta = number(mapping(r.get('tracking')).get('delta_close_pp'))
        if delta is None and a is not None and c is not None:
            delta = c - a
        weak_rows.append([
            _esc(_display_name(r, 10)),
            f'{pct(a)} → <b style="color:#16875b">{pct(c)}</b>',
            f'<span style="color:#16875b">{pp(delta)}</span>',
        ])

    ladder_sec = mapping(report.get('ladder_section'))
    yest_counts = _rows(ladder_sec.get('yesterday_board_counts')) or _rows(ladder_sec.get('summary'))
    ladder_rows = [r for r in _rows(ladder_sec.get('rows')) if 'ST' not in str(r.get('name') or '').upper()]
    from .auction_theme_buckets import normalize_theme_label

    def _is_promoted(row: Mapping[str, Any]) -> bool:
        c = number(row.get('close_pct'))
        if c is None:
            return False
        limit_pct = number(row.get('limit_pct'))
        if limit_pct:
            return c >= limit_pct - 0.5
        return c >= 19.5 or (9.5 <= c <= 10.6)

    def _streak_level(row: Mapping[str, Any]) -> int:
        s = number(row.get('prior_streak'))
        return int(s) if s is not None and s > 0 else 0

    col_defs = [
        ('更高(昨≥4板)', lambda lvl: lvl >= 4),
        ('4板(昨3板)', lambda lvl: lvl == 3),
        ('3板(昨2板)', lambda lvl: lvl == 2),
        ('2板(昨1板)', lambda lvl: lvl == 1),
    ]
    if first_boards:
        col_defs.append(('首板(新涨停)', lambda lvl: lvl == 0))

    # Per-tier promotion rate strip
    tier_stats = []
    for label, pred in col_defs:
        if label.startswith('首板'):
            tier_stats.append(f'新首板 <b style="color:#bb3541">{len(first_boards)}</b>只')
            continue
        bucket = [r for r in ladder_rows if pred(_streak_level(r))]
        if not bucket:
            continue
        prom = sum(1 for r in bucket if _is_promoted(r))
        rate = f'{prom / len(bucket) * 100:.0f}%'
        tier_stats.append(f'{label.split("(")[1].rstrip(")")} <b style="color:{"#bb3541" if prom else "#53677e"}">{prom}/{len(bucket)}</b> ({rate})')
    if not tier_stats and yest_counts:
        tier_stats = [
            f'{_esc(str(x.get("board_height") or "—"))}:{int(x.get("count") if x.get("count") is not None else x.get("previous_n"))}'
            for x in sorted(yest_counts, key=lambda item: -int(re.search(r"\d+", str(item.get("board_height") or "0")).group(0)))
            if (x.get('count') is not None or x.get('previous_n') is not None)
        ]

    # Group ladder rows (+ today's new 1st-boards) by canonical theme for 2D matrix
    theme_groups: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for r in ladder_rows:
        lvl = _streak_level(r)
        if lvl < 1:
            continue
        raw_theme = str(r.get('primary_reason') or r.get('main_plate') or '').strip()
        theme = normalize_theme_label(raw_theme) if raw_theme else ''
        theme = theme or '未归类'
        theme_groups.setdefault(theme, []).append((lvl, r))
    for fb in first_boards:
        raw_theme = str(fb.get('main_plate') or '').strip()
        theme = normalize_theme_label(raw_theme) if raw_theme else ''
        theme = theme or '未归类'
        theme_groups.setdefault(theme, []).append((0, fb))

    ordered_themes = sorted(
        theme_groups.items(),
        key=lambda g: (
            g[0] == '未归类',
            -max((lvl for lvl, _ in g[1]), default=0),
            -sum(1 for lvl, r in g[1] if lvl >= 1 and _is_promoted(r)),
            -sum(1 for lvl, _ in g[1] if lvl == 0),
            -len(g[1]),
            g[0],
        ),
    )
    chosen_themes, omitted_themes = ordered_themes[:8], ordered_themes[8:]
    if omitted_themes:
        other_rows = [pair for _, pairs in omitted_themes for pair in pairs]
        chosen_themes.append(('其他题材', other_rows))

    def _short_pct(val: Any) -> str:
        n = number(val)
        return f'{n:+.1f}%' if n is not None else '—'

    if chosen_themes:
        m_headers = ['主线题材', *[lbl for lbl, _ in col_defs]]
        th_html = ''.join(f'<th style="text-align:left;padding:5px 4px;background:#edf3fa;border:1px solid #dde5ef">{_esc(h)}</th>' for h in m_headers)
        tr_list = [f'<tr>{th_html}</tr>']
        for theme_name, pairs in chosen_themes:
            cells_html = [f'<td style="vertical-align:top;padding:5px 4px;border:1px solid #e8edf4;font-weight:700;background:#fafbfd">{_esc(theme_name)}</td>']
            for _, pred in col_defs:
                cell_items = [(lvl, r) for lvl, r in pairs if pred(lvl)]
                cell_items.sort(key=lambda pair: (pair[0] != 0 and not _is_promoted(pair[1]), -(number(pair[1].get('close_pct')) or -999)))
                visible, hidden = cell_items[:3], cell_items[3:]
                cards_in_cell = []
                for lvl, r in visible:
                    a = number(_first(r, 'p0925_pct', 'auction_pct'))
                    c = number(r.get('close_pct'))
                    if lvl == 0 or _is_promoted(r):
                        color, bg = '#bb3541', '#fff0f0'
                    elif c is not None and c >= 0:
                        color, bg = '#bd6a20', '#fff4e5'
                    elif c is not None:
                        color, bg = '#16875b', '#eaf8ef'
                    else:
                        color, bg = '#5c6d80', '#f2f5f8'
                    track_str = f'{_short_pct(a)}→{_short_pct(c)}' if a is not None else pct(c)
                    cards_in_cell.append(
                        f'<div style="background:{bg};border-left:3px solid {color};padding:2px 4px;margin:2px 0">'
                        f'<b>{_esc(_display_name(r, 6))}</b><br><span style="color:{color};font-weight:700">{track_str}</span></div>'
                    )
                if hidden:
                    prom_h = sum(1 for lvl, r in hidden if lvl == 0 or _is_promoted(r))
                    cards_in_cell.append(f'<div style="color:#667085">+{len(hidden)}只(晋{prom_h})</div>')
                cells_html.append('<td style="vertical-align:top;padding:3px;border:1px solid #e8edf4">' + (''.join(cards_in_cell) or '<span style="color:#cbd5e1">—</span>') + '</td>')
            tr_list.append(f'<tr data-theme="{_esc(theme_name)}">{"".join(cells_html)}</tr>')
        ladder_matrix_html = f'<table data-matrix="ladder-2d" width="100%" cellspacing="0" style="table-layout:fixed;border-collapse:collapse;font-size:11px;line-height:1.25">{"".join(tr_list)}</table>'
    else:
        ladder_matrix_html = '<div>数据缺口：未生成</div>'

    sections = (
        f'<header style="background:#142c4a;color:white;padding:15px">'
        f'<b style="font-size:20px">收盘复盘 · 竞价→收盘闭环</b><br>'
        f'{_esc(trade_date)} · 收盘｜{_esc(regime)} · 竞价 {auction_med} → 收盘 {close_med}</header>'
        f'<table width="100%" cellspacing="4"><tr>{card_html}</tr></table>'
        f'<section data-section="close-plates" style="background:white;padding:12px 14px;margin:8px 0">'
        f'<h2 style="margin:0 0 8px;font-size:16px">{plate_section_title}</h2>'
        f'{plate_table_html}{plate_footer_html}</section>'
        f'<section data-section="close-verification" style="background:white;padding:12px 14px;margin:8px 0">'
        f'<h2 style="margin:0 0 8px;font-size:16px">② 全天兑现与核按钮（竞价焦点验收）</h2>'
        f'<h3 style="margin:8px 0 6px;font-size:14px">🚀 日内最强兑现 / 修复 Top 5</h3>'
        f'{table(["股票", "竞价 → 收盘", "Δ变化"], rebound_rows, "close-rebound-row")}'
        f'<h3 style="margin:12px 0 6px;font-size:14px">⚠️ 日内回撤 / 砸盘避坑 Top 5</h3>'
        f'{table(["股票", "竞价 → 收盘", "Δ回撤"], weak_rows, "close-weak-row")}'
        + (f'<div style="margin-top:8px;font-size:12px;color:#53677e">新股/无涨跌幅限制：{unlimited_note}</div>' if unlimited_note else '')
        + f'</section>'
        f'<section data-section="close-ladder" style="background:white;padding:12px 14px;margin:8px 0">'
        f'<h2 style="margin:0 0 6px;font-size:16px">③ 连板高标二维矩阵（主线 × 板高｜竞价→收盘）</h2>'
        + (f'<div style="font-size:12px;color:#53677e;margin-bottom:8px">晋级率：{" ｜ ".join(tier_stats)}</div>' if tier_stats else '')
        + f'{ladder_matrix_html}</section>'
        f'<footer style="padding:8px 4px;color:#667085;font-size:11px">'
        f'口径：涨停/跌停为收盘价代理；缺失显示“—”不补零；仅客观复盘，不构成买卖建议。</footer>'
    )
    plain = html.unescape(re.sub(r'<[^>]+>', ' ', sections))
    return sections, re.sub(r'\s+', ' ', plain).strip()


def render_email_board(report: Mapping[str, Any], stage: str, trade_date: str) -> tuple[str, str]:
    """Return a compact complete email HTML document and a matching text view."""
    stage_key = "close" if any(x in stage.lower() for x in ("close", "closing", "eod")) else "open" if "open" in stage.lower() else "auction"
    stage_label = {"close": "收盘", "open": "开盘", "auction": "竞价"}[stage_key]
    if stage_key == "close":
        body, plain = _render_close_v3_board(report, trade_date)
        document = ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>'
                    '<body style="margin:0;background:#f5f7fa"><div style="max-width:680px;margin:auto;color:#263445;font-family:-apple-system,\'PingFang SC\',\'Microsoft YaHei\',Arial,sans-serif;line-height:1.5">' + body + '</div></body></html>')
        document = re.sub(r"(?<![#\da-fA-F])\d{6}(?!\d)", "", document)
        plain = re.sub(r"(?<!\d)\d{6}(?!\d)", "", plain)
        if len(document.encode("utf-8")) >= 40_000:
            raise ValueError(f"rendered close email exceeds limit ({len(document.encode('utf-8'))} bytes)")
        return document, plain
    if stage_key == "open":
        body, plain = _render_open_verification_board(report, trade_date)
        document = ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>'
                    '<body style="margin:0;background:#f5f7fa"><div style="max-width:680px;margin:auto;color:#263445;font-family:Arial,sans-serif;line-height:1.5">' + body + '</div></body></html>')
        for forbidden in ("close_pct", "close_mean", "close_median", "收盘"):
            document = document.replace(forbidden, "")
            plain = plain.replace(forbidden, "")
        document = re.sub(r"(?<!\d)\d{6}(?!\d)", "", document)
        plain = re.sub(r"(?<!\d)\d{6}(?!\d)", "", plain)
        if len(document.encode("utf-8")) >= 30_000:
            raise ValueError(f"rendered open email exceeds limit ({len(document.encode('utf-8'))} bytes)")
        return document, plain
    tiles = _tiles(report, stage_key)
    tile_html = ("<table role=\"presentation\" width=\"100%\" cellspacing=\"3\" cellpadding=\"0\"><tr>" + "".join(x[1] for x in tiles) + "</tr></table>") if tiles else "<div>数据缺口：情绪温度计字段不足</div>"
    ladder_html, ladder_text = _ladder(report, stage_key)
    extreme_html, extreme_text = _extremes(report, stage_key)
    plate_html, plate_text = _plates(report, stage_key)
    ab_html, ab_text = _a_to_b(report, stage_key)
    other_html, other_text = _other(report, stage_key)
    _story, story_html, story_text = _story_view(report, stage_key, trade_date)
    front_html, front_text = _front_page_view(report, stage_key)
    open_html, open_text = _render_open_verification_board(report, trade_date) if stage_key == "open" else ("", "")
    def assemble(extreme_html: str, ab_html: str) -> str:
        appendix_html = (f'<section style="margin:12px 0"><h2 style="font-size:13px;color:#475467">附表：完整事实章节</h2>'
                         f'{_section("情绪温度计", tile_html)}{ladder_html}{extreme_html}{ab_html}{other_html}</section>')
        main_html = (f'{story_html}{plate_html}{appendix_html}' if stage_key == "close" else
                     f'{open_html if stage_key == "open" else front_html + plate_html}<details style="margin:8px 0"><summary style="padding:8px;background:#e9eef5">附表（极值、完整梯队、盘面、A→B、温度计）</summary>{front_html + plate_html if stage_key == "open" else ""}{appendix_html}</details>')
        body = (f'<div style="max-width:680px;margin:0 auto;background:#f5f7fa;color:#263445;font-family:Arial,"Microsoft YaHei",sans-serif;font-size:12px;line-height:1.45;font-variant-numeric:tabular-nums">'
                f'<header style="padding:10px 2px"><b>{_esc(trade_date)} {stage_label}盘面</b></header>'
                f'{main_html}'
                f'<footer style="padding:8px;color:#667085;font-size:10px">归属来源：{_esc(str((_front_page_inputs(report, stage_key).get("ladder") or {}).get("attribution_basis") or "登记表回退"))}；缺失不补零；涨停状态为价格代理；单日事实不构成预测或建议。</footer></div>')
        document = '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="margin:0;background:#f5f7fa">' + body + "</body></html>"
        return re.sub(r"(?<!\d)\d{6}(?!\d)", "", document) if stage_key != "close" else document

    max_bytes = 120 * 1024 if stage_key == "close" else 89_999 if stage_key == "open" else 100_000
    document = assemble(extreme_html, ab_html)
    if stage_key != "close" and len(document.encode("utf-8")) > max_bytes:
        entries = _rows(report.get("a_to_b"))
        entry_count = len(entries) if any(e.get("schema_version") == 2 for e in entries) else min(8, len(entries))
        def fit(count, render, fixed, ab_first):
            low, high = 0, count
            while low < high:
                mid = (low + high + 1) // 2
                candidate, _ = render(mid)
                doc = assemble(fixed, candidate) if ab_first else assemble(candidate, fixed)
                if len(doc.encode("utf-8")) <= max_bytes:
                    low = mid
                else:
                    high = mid - 1
            return render(low)
        ab_html, ab_text = fit(entry_count, lambda n: _a_to_b(report, stage_key, n), extreme_html, True)
        document = assemble(extreme_html, ab_html)
        if len(document.encode("utf-8")) > max_bytes:
            attached = report.get("extremes_board")
            if not isinstance(attached, Mapping):
                chapters = report.get("chapters") if isinstance(report.get("chapters"), Mapping) else {}
                attached = chapters.get("extremes_board")
            if not isinstance(attached, Mapping):
                attached = build_extremes_board(_rows(report.get("stocks")), stage=stage_key,
                                                checkpoint=_open_checkpoint_label(report) if stage_key == "open" else None)
            board = attached.get(stage_key) if isinstance(attached, Mapping) else None
            rows = board if isinstance(board, list) else board.get("rows", []) if isinstance(board, Mapping) else []
            extreme_html, extreme_text = fit(len(rows), lambda n: _extremes(report, stage_key, n), ab_html, False)
            document = assemble(extreme_html, ab_html)
    if stage_key != "close":
        for forbidden in ("close_pct", "close_mean", "close_median", "收盘%", "收盘中位", "收盘"):
            assert forbidden not in ab_html
            assert forbidden not in ab_text
    tile_text = []
    for label, markup in tiles:
        value = html.unescape(re.sub(r"<[^>]*>", "", markup)).strip()
        if value.startswith(label):
            value = value[len(label):].strip()
        tile_text.append(f"{label} {value}".strip())
    plain_main = open_text + "\n" + front_text if stage_key == "open" else front_text if stage_key != "close" else story_text
    appendix_story = story_text if stage_key == "close" else "故事摘要见首屏"
    plain = (f"{trade_date} {stage_label}盘面\n{plain_main}\n\n附表：完整事实章节\n{appendix_story}\n情绪温度计：" +
             "；".join(tile_text) +
             f"\n{ladder_text}\n{extreme_text}\n{plate_text}\n{ab_text}\n数据与口径：{other_text}")
    if stage_key == "open":
        for forbidden in ("close_pct", "close_mean", "close_median", "收盘"):
            document = document.replace(forbidden, "")
            plain = plain.replace(forbidden, "")
    if stage_key != "close":
        document = re.sub(r"(?<!\d)\d{6}(?!\d)", "", document)
        plain = re.sub(r"(?<!\d)\d{6}(?!\d)", "", plain)
    max_bytes = 120 * 1024 if stage_key == "close" else 89_999 if stage_key == "open" else 100_000
    if len(document.encode("utf-8")) > max_bytes:
        raise ValueError(f"rendered email exceeds {max_bytes} bytes ({len(document.encode('utf-8'))} bytes)")
    return document, plain
