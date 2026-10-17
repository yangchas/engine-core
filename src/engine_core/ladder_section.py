"""Compact descriptive ladder section; all close outcomes are price proxies."""
from __future__ import annotations

from collections import Counter
from statistics import median
from typing import Any, Mapping, Sequence

from .a_to_b import _prior_streak
from .limit_price import state_at_close

PATH_LABELS = {
    "LIMIT_HOLD": "采样仍在涨停价", "STRONG_FADE": "竞价偏高后回落",
    "WEAK_REBOUND": "竞价偏低后翻红回升", "WEAK_CONTINUE": "竞价偏低后续弱",
    "FLAT_PUSH": "非高开后抬升", "OPEN_REPAIR": "开盘先回撤后回升",
    "OTHER": "其他交错路径", "UNKNOWN": "信息不足，暂不归类",
}


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _emotion_bucket(stocks: Sequence[Mapping[str, Any]], asof: str) -> dict[str, Any]:
    """Apply C184's fixed open-to-0945 descriptive buckets when covered."""
    clock = asof[11:16] if len(asof) >= 16 else ""
    if clock and clock < "09:45":
        return {"state": "UNKNOWN", "label": "未到09:45截点", "asof": asof}
    total = len(stocks)
    if not total:
        return {"state": "UNKNOWN", "label": "无全市场样本", "asof": asof}
    samples: dict[str, list[float]] = {"p0932_pct": [], "p0945_pct": []}
    for row in stocks:
        for key in samples:
            value = row.get(key)
            if isinstance(value, (int, float)):
                samples[key].append(float(value))
    coverage = {key: len(values) / total for key, values in samples.items()}
    if min(coverage.values()) < 0.8:
        return {"state": "UNKNOWN", "label": "价格覆盖不足80%", "asof": asof,
                "coverage_0932": coverage["p0932_pct"], "coverage_0945": coverage["p0945_pct"]}
    med32, med45 = median(samples["p0932_pct"]), median(samples["p0945_pct"])
    up32 = sum(value > 0 for value in samples["p0932_pct"]) / len(samples["p0932_pct"])
    up45 = sum(value > 0 for value in samples["p0945_pct"]) / len(samples["p0945_pct"])
    delta_median, delta_breadth = med45 - med32, up45 - up32
    weak_start = med32 <= -0.5 or up32 <= 0.35
    strong_start = med32 >= 0.5 or up32 >= 0.60
    if weak_start and delta_median >= 0.5 and delta_breadth >= 0.10:
        state = "WEAK_REPAIR"
    elif strong_start and delta_median <= -0.5 and delta_breadth <= -0.10:
        state = "STRONG_FADE"
    elif weak_start and delta_median <= 0 and delta_breadth <= 0:
        state = "WEAK_CONTINUE"
    elif strong_start and med45 >= 0 and up45 >= 0.50:
        state = "STRONG_HOLD"
    else:
        state = "MIXED"
    labels = {"WEAK_REPAIR": "弱后修复", "STRONG_FADE": "强后回落",
              "WEAK_CONTINUE": "弱延续", "STRONG_HOLD": "强保持", "MIXED": "混合"}
    return {"state": state, "label": labels[state], "asof": asof,
            "coverage_0932": coverage["p0932_pct"], "coverage_0945": coverage["p0945_pct"],
            "start_checkpoint": "0932（研究基准为0931，在线最近检查点近似）",
            "median_0932_pct": med32, "median_0945_pct": med45,
            "up_share_0932": up32, "up_share_0945": up45,
            "price_proxy_note": "14日描述分档；在线起点取0932最近检查点近似0931，不是预测"}


def build_ladder_section(stocks: Sequence[Mapping[str, Any]], *, stage: str,
                         asof: str, frequencies: Mapping[str, Mapping[str, Any]] | None = None,
                         summary: Sequence[Mapping[str, Any]] = (),
                         market_stocks: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    stage_key = stage.lower()
    close_visible = stage_key in {"close", "closing", "eod"}
    auction_only = stage_key in {"auction", "auction_0926", "auction_0925"}
    market_by_symbol = {str(row.get("symbol", row.get("code", ""))).zfill(6): row
                        for row in (market_stocks or ())}
    rows = []
    for source in stocks:
        row = dict(source)
        market_row = market_by_symbol.get(str(row.get("symbol", row.get("code", ""))).zfill(6), {})
        for key in ("limit_pct", "high_pct", "low_pct", "auction_amount_yuan", "in_yest_pool", "lb_days"):
            if row.get(key) is None and market_row.get(key) is not None:
                row[key] = market_row[key]
        streak, streak_conflict = _prior_streak(row)
        path = ("UNKNOWN" if auction_only else
                str(row.get("path_type", row.get("path_label", row.get("path_flags", "UNKNOWN"))) or "UNKNOWN"))
        if path not in PATH_LABELS:
            path = "UNKNOWN"
        freq = (frequencies or {}).get(path, {})
        n, k, days = _int(freq.get("n")), _int(freq.get("k")), _int(freq.get("days"))
        hist = ("竞价截点尚无开盘路径分类" if auction_only else
                f"14日描述：收盘仍在涨停价 {k}/{n}；非预测" if n is not None and n >= 30 and days is not None and days >= 5
                else f"样本不足：{k if k is not None else '—'}/{n if n is not None else '—'}，{days if days is not None else '—'}日；暂不提供稳定频率")
        item = {"symbol": str(row.get("symbol", row.get("code", ""))).zfill(6),
                "name": row.get("name"), "prior_streak": streak,
                "prior_streak_status": streak_conflict or ("已知" if streak is not None else "—"),
                "main_plate": row.get("main_plate"),
                "p0925_pct": row.get("p0925_pct", row.get("auction_pct", row.get("0925"))),
                "p0932_pct": None if auction_only else row.get("p0932_pct", row.get("0932")),
                "p0945_pct": None if auction_only else row.get("p0945_pct", row.get("0945")),
                "path_type": path, "path_label": PATH_LABELS[path],
                "history_frequency": hist, "asof": asof,
                "limit_status": "收盘涨停价代理；封单状态未知" if close_visible else "涨停价代理；封单状态未知"}
        item.update({key: row.get(key) for key in ("limit_pct", "high_pct", "auction_amount_yuan", "in_yest_pool")})
        if close_visible:
            item["close_pct"] = row.get("close_pct", row.get("close_result_pct"))
            item["limit_status"] = state_at_close(
                row.get("close"), row.get("high"), row.get("low"), row.get("pre_close"),
                item["symbol"], is_st=bool(row.get("is_st")),
                unlimited=bool(row.get("is_unlimited_price_band") or row.get("unlimited_price_band")))
        rows.append(item)
    by_streak = Counter(r["prior_streak"] for r in rows if r["prior_streak"] is not None)
    market_by_symbol = {str(row.get("symbol", row.get("code", ""))).zfill(6): row
                        for row in (market_stocks or ())}
    def exact_state(row: Mapping[str, Any], prefix: str = "") -> str:
        source = market_by_symbol.get(str(row.get("symbol", "")), row)
        fallback = "" if not prefix else None
        return state_at_close(source.get(prefix + "close", source.get(fallback + "close") if fallback is not None else None),
                              source.get(prefix + "high", source.get(fallback + "high") if fallback is not None else None),
                              source.get(prefix + "low", source.get(fallback + "low") if fallback is not None else None),
                              source.get("pre_close"), str(row.get("symbol", "")),
                              is_st=bool(source.get("is_st")),
                              unlimited=bool(source.get("is_unlimited_price_band") or
                                             source.get("unlimited_price_band")))
    price_rows = list(market_stocks or stocks)
    if close_visible:
        states = [exact_state(r) for r in price_rows]
    elif not auction_only:
        states = [exact_state(r, "p0945_") for r in price_rows]
    else:
        states = []
    known_states = [state for state in states if state != "缺价格字段"]
    count_status = ("NOT_APPLICABLE" if auction_only else
                    "MISSING" if not price_rows else
                    "READY" if len(known_states) == len(states) else
                    "PARTIAL" if known_states else "MISSING")
    exact_up_count = sum(state == "封(价格代理)" for state in known_states)
    exact_down_count = sum(state == "跌停价" for state in known_states)
    exact_touch_count = sum(state in {"触", "跌触"} for state in known_states)
    close_up = exact_up_count if count_status == "READY" else None
    current = exact_up_count if count_status == "READY" and not close_visible and not auction_only else None
    summary_counts = [{"board_height": row.get("board_height"), "count": _int(row.get("previous_n"))}
                      for row in summary if row.get("board_height") and _int(row.get("previous_n")) is not None]
    yesterday_counts = summary_counts or [{"board_height": f"{level}板", "count": count}
                                         for level, count in sorted(by_streak.items())]
    has_pool_field = any("in_yest_pool" in row for row in (market_stocks or ()))
    pool_rows = [row for row in (market_stocks or ()) if row.get("in_yest_pool") is True or
                 row.get("in_yest_pool") in (1, "1")]
    promotion_by_level: dict[str, dict[str, int]] = {}
    if has_pool_field:
        pool_counts = Counter(_int(row.get("lb_days")) for row in pool_rows if _int(row.get("lb_days")) is not None)
        for level, total in pool_counts.items():
            promoted = 0
            for row in pool_rows:
                if _int(row.get("lb_days")) != level:
                    continue
                if exact_state(row) == "封(价格代理)":
                    promoted += 1
            promotion_by_level[f"{level}板"] = {"promoted": promoted, "total": total}
    emotion = (_emotion_bucket(market_stocks, asof) if not auction_only and market_stocks is not None
               else {"state": "UNKNOWN", "label": "无09:45全市场截面", "asof": asof})
    result = {"asof": asof, "stage": stage, "rows": rows,
              "yesterday_board_counts": yesterday_counts,
              "today_promotion_or_price_proxy_count": close_up if close_visible else current,
              "today_limit_down_price_count": exact_down_count if count_status == "READY" else None,
              "today_touched_limit_count": exact_touch_count if count_status == "READY" else None,
              "price_count_status": count_status,
              "count_label": "收盘涨停价代理家数" if close_visible else "截至0945涨停价代理家数",
              "summary": [dict(r) for r in summary],
              "promotion_status": "READY" if has_pool_field and pool_rows and promotion_by_level
              else "MISSING_YESTERDAY_POOL",
              "promotion_by_previous_level": promotion_by_level,
              "emotion_asof": "0945前可见状态；研究分档为14日描述，非预测",
              "emotion": emotion,
              "postmarket_label": "事后对照，非开盘信息" if close_visible else None}
    return result
