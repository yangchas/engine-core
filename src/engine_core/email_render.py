"""Structured, email-safe rendering for Core daily reports."""
from __future__ import annotations

import html
import json
import math
import re
from typing import Any, Mapping

from .email_board import render_email_board

RENDER_VERSION = "core-mail-v4"
MAX_EMAIL_HTML_BYTES = 100_000
_RED = "#c83b3b"
_GREEN = "#16835d"


def _v6_failure_category(raw: Any) -> str:
    name = str(raw).split(":", 1)[0].strip().lower()
    if "permission" in name or "accessdenied" in name:
        return "PERMISSION"
    if "missing" in name or "notfound" in name or "filenotfound" in name:
        return "DATA_MISSING"
    if "timeout" in name:
        return "TIMEOUT"
    return "INTERNAL_ERROR"


def v6_failure_public_code(raw: Any) -> str:
    """Return a stable, path-free public diagnostic for a V6 failure."""
    match = re.fullmatch(r"V6_PANORAMA_MISSING:(PERMISSION|DATA_MISSING|TIMEOUT|INTERNAL_ERROR)", str(raw))
    if match:
        return str(raw)
    return "V6_PANORAMA_MISSING:" + _v6_failure_category(raw)


def v6_failure_internal(raw: Any) -> str:
    """Keep useful internal context while removing filesystem locations."""
    text = re.sub(r"[A-Za-z]:\\(?:[^\\\s<>\"']+\\?)*[^\\\s<>\"']*", "<path>", str(raw))
    text = re.sub(r"(?<![\w.])/(?:home|tmp|var/tmp|private/tmp|Users)/[^\s<>\"']+", "<path>", text)
    return text[:500]

# User-visible fields are allow-listed. Unknown machine fields are omitted.
FIELD_LABELS = {
    "symbol": None, "a_symbol": None, "name": "名称", "stock_name": "名称", "main_plate": "所属板块",
    "count": "数量", "direction": "方向", "mean_pct": "平均涨幅", "auction_pct": "竞价涨幅",
    "close_pct": "收盘涨幅", "change_pp": "变化", "limit_band": "板数",
    "plates": "所属板块", "limit_pct": "板幅", "high_pct": "日内最高", "low_pct": "日内最低",
    "auction_amount_bp": "占大盘竞价额", "price_band_label": "价格带标记", "is_new_listing": "价格带例外", "unlimited_price_band": "价格带例外",
    "plate": "板块", "members": "成员数", "streak": "连板数", "lb_days": "连板数",
    "status": "状态", "close_pct": "收盘涨幅", "pct": "涨跌幅", "pct_change": "涨跌幅",
    "auction_pct": "竞价涨幅", "p0925_pct": "竞价涨幅", "p0932_pct": "09:32涨幅",
    "p0945_pct": "09:45涨幅", "open_pct": "开盘涨幅", "delta_pp": "较竞价变化",
    "delta_over_limit": "收盘变化÷板幅", "auction_amount_yuan": "竞价额",
    "amount_yuan": "成交额", "match_amount_yuan": "竞价额", "match_yuan": "竞价额",
    "amount": "成交额", "amount_share": "成交额占比", "amount_share_pct": "成交额占比",
    "amount_share_bp": "成交额占比", "auction_amount_share": "竞价额占比",
    "auction_amount_bp": "占大盘竞价额",
    "in_yest_pool": "昨日涨停池", "visibility": "可见时点",
    "visibility_half_limit": "0.5L可见时点",
    "auction_amount_rank": "竞价额排名", "rank": "排名", "score": "得分",
    "close_mean": "收盘平均涨幅", "close_median": "收盘中位涨幅",
    "auction_mean": "竞价平均涨幅", "auction_median": "竞价中位涨幅",
    "close_ge_5": "收盘涨幅≥5%", "sealed_up": "涨停价数量", "turned_negative": "收盘转负数量",
    "close_ge_5_rate": "收盘涨幅≥5%占比", "sealed_up_rate": "涨停价占比",
    "turned_negative_rate": "收盘转负占比", "n": "样本数", "matched": "匹配数",
    "close_limit_up_count": "收盘涨停价代理家数", "close_limit_down_count": "收盘跌停价代理家数",
    "up_count": "上涨家数", "down_count": "下跌家数", "flat_count": "平盘家数",
    "limit_pct": "涨跌停幅度", "auction_price_status": "竞价价格类型",
    "prior_range_quintile": "20日区间位置",
    "p0945_cumulative_amount_yuan": "0945累计额",
    "in_yest_pool": "昨日涨停池", "prior_limit_streak": "此前连板数",
    "prior_down_streak": "此前连跌数", "visibility": "出现阶段", "visibility_half_limit": "0.5L可见时点",
    "rest_bid_yuan": "未匹配买额", "rest_ask_yuan": "未匹配卖额",
    "close_at_limit_up": "收盘触及涨停价", "is_big_meat": "大肉", "is_big_loss": "大面",
    "rel_to_strongest_plate": "相对最强板块涨幅", "move_0924_0925_pp": "09:24至09:25变化",
    "auction_amount_top10": "竞价额前十", "close_top10": "收盘强势板块前十",
    "close_bottom10": "收盘弱势板块前十", "yesterday_highboards": "昨日高标",
    "sealed_up": "收盘价=涨停价(价格代理)", "sealed_down": "收盘价=跌停价(价格代理)", "emerging": "新冒头",
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
    "p0925_mean": "板块竞价均值", "p0932_mean": "板块09:32均值", "p0945_mean": "板块09:45均值",
    "relative_to_plate_pp": "个股相对板块", "position": "板块内位置", "peer_top3": "板块同伴Top3",
    "amount_64d_median_yuan": "64日成交额中位数", "amount_vs_64d_median": "对64日中位倍数",
    "amount_vs_64d_median_common": "全天成交额/64日中位数倍数",
    "amount_64d_common_pool_n": "固定共同池标的数",
    "amount_64d_percentile": "64日全市场成交额分位", "amount_64d_percentile_common": "64日固定共同池成交额分位", "regime_label": "描述性日型",
    "regime_history_sample_days": "历史样本日数", "same_regime_days": "同日型样本数",
    "previous_space_height": "昨日最高板", "max_price_streak": "今日最高板高",
    "top_symbols": "最高板股票", "first_board_count": "今日首板数",
    "previous_limit_n": "昨日涨停家数", "promoted_n": "晋级家数", "broken_n": "断板家数",
    "promotion_rate": "晋级率", "unknown_broken_n": "炸板路径未知数",
    "board_height": "昨日板数", "current_n": "今日收盘涨停价数", "previous_n": "昨日家数",
    "a_symbol": "A代码", "a_name": "A名称", "a_prior_streak": "A昨日连板",
    "a_status": "A状态", "reason": "未成立原因", "candidates": "B候选",
    "prior_streak_status": "昨日板数状态", "path_type": "路径类型", "path_label": "路径描述",
    "history_frequency": "14日描述频率", "limit_status": "价格状态代理",
    "asof": "数据时点", "relation_asof": "关系时点", "yesterday_board_counts": "昨日板数分布",
    "emotion_label": "0945情绪档", "coverage_0945": "0945价格覆盖",
    "today_promotion_or_price_proxy_count": "今日晋级/涨停价代理数", "count_label": "家数口径",
    "summary": "梯队摘要", "emotion_asof": "情绪口径", "postmarket_label": "盘后对照",
    "market_amount_percentile": "64日分位", "market_amount_multiple": "64日倍数",
    "pool": "统计池", "top10": "竞价额前十", "top20": "竞价额前二十",
    "sample_days": "历史样本日数",
    "p0925_mean": "09:25平均涨幅", "p0932_mean": "09:32平均涨幅",
    "p0945_mean": "09:45平均涨幅",
    "median_yi": "64日竞价额中位数(亿)", "multiple": "对64日中位数倍数",
    "percentile": "64日历史分位", "pool_label": "统计池",
    "auction_baseline_multiple": "竞价额对64日基线倍数",
    "auction_baseline_percentile": "竞价额历史分位",
}

# Explicit number semantics for report fields. Suffix classes below are only
# used for the documented semantic suffixes, never arbitrary key substrings.
FIELD_FORMATS = {
    "ratio": frozenset({"amount_64d_percentile", "amount_64d_percentile_common", "close_ge_5_rate", "sealed_up_rate",
                        "turned_negative_rate", "promotion_rate", "auction_amount_share",
                        "auction_amount_share_pct", "auction_amount_share_ratio", "auction_pct_coverage",
                        "anchor_coverage", "source_coverage", "coverage_rate",
                        "retention_rate", "success_rate", "up_share", "down_share", "rate",
                        "share", "regime_history_percentile", "top100_concentration_pct",
                        "p0925_range_position20_pct"}),
    "pp": frozenset({"relative_to_plate_pp", "delta_pp", "change_pp", "close_vs_p0945_pp",
                     "move_0924_0925_pp"}),
    "multiple": frozenset({"auction_baseline_multiple", "amount_vs_prev_common", "amount_vs_64d_median_common",
                           "amount_vs_prev", "amount_vs_64d_median", "vs_amount", "vs_previous_day_final",
                           "vs_amount_baseline", "amount_weighted_multiple"}),
    "count": frozenset({"count", "n", "matched", "members", "up_count", "down_count", "flat_count",
                        "close_limit_up_count", "close_limit_down_count", "final_count", "indicative_count",
                        "amount_common_pool_n", "amount_64d_common_pool_n", "amount_coverage", "close_coverage",
                        "auction_pct_coverage", "close_pct_coverage", "p0932_coverage", "p0945_coverage"}),
    "amount": frozenset({"amount_yuan", "auction_amount_yuan", "p0945_cumulative_amount_yuan",
                         "amount_64d_median_yuan", "auction_amount_top10"}),
    "percent": frozenset({"p0925_range_position20_pct"}),
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
    if key_l in FIELD_FORMATS["percent"]:
        return f"{x:.1f}%"
    if key_l == "auction_baseline_percentile":
        return f"{x:.2f}%"
    if key_l in FIELD_FORMATS["count"]:
        return f"{int(x):,}" if x.is_integer() else f"{x:,.2f}"
    if key_l in FIELD_FORMATS["ratio"] or key_l.endswith(("_rate", "_share", "_coverage", "_percentile", "_ratio")):
        return f"{x * 100:.1f}%"
    if key_l in FIELD_FORMATS["amount"] or key_l.endswith(("_amount_yuan", "_amt_yuan")) or key == "竞价额":
        if abs(x) >= 100_000_000:
            return f"{x / 100_000_000:,.2f} 亿"
        if abs(x) >= 10_000:
            return f"{x / 10_000:,.2f} 万"
    if key_l.endswith("_bp"):
        return f"{x:+.1f} bp"
    if key_l == "delta_over_limit":
        return f"{x:+.2f}× 板幅"
    if key_l in FIELD_FORMATS["pp"] or key_l.endswith("_pp"):
        return f"{x:+.2f} pp"
    if key_l.endswith("_pct") or key_l in {"close_median", "p0945_median", "auction_median", "amount_weighted_pct_change", "pct", "pct_change"}:
        return f"{x:+.2f}%" if abs(x) < 1000 else f"{x:,.2f}"
    if key_l in FIELD_FORMATS["multiple"] or key_l.startswith("vs_"):
        return f"{x:.2f}×"
    return f"{x:,.2f}" if not x.is_integer() else f"{int(x):,}"


def _plain_value(value: Any, key: str = "") -> str:
    if key == "in_yest_pool" and isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)) or value is None:
        return _num(value, key)
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value if item not in (None, "")]
        if not items:
            return "—"
        shown = "、".join(items[:3])
        return _esc(shown + (f" 等{len(items)}个" if len(items) > 3 else ""))
    return _esc(value)


def _label(key: str) -> str | None:
    return FIELD_LABELS.get(str(key))


def _tone(key: str, value: Any) -> tuple[str, str]:
    name = key.lower()
    is_change = any(token in name for token in ("pct", "change", "delta", "涨幅", "pp")) or name == "auction_bp"
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
        if market.get("auction_median") is not None:
            points.append(f"竞价中位 {_num(market['auction_median'], 'auction_median')}；收盘较竞价变化 {_num(market.get('close_median') - market['auction_median'] if market.get('close_median') is not None else None, 'delta_pp')}。")
        if market.get("p0945_median") is not None and market.get("close_median") is not None:
            points.append(f"09:45中位 {_num(market['p0945_median'], 'p0945_median')}，收盘较09:45变化 {_num(market.get('close_vs_p0945_pp'), 'delta_pp')}；日型按预设规则为 {market.get('regime_label', '未知')}。")
        if market.get("up_count") is not None or market.get("down_count") is not None:
            points.append(f"上涨 {_num(market.get('up_count'))} 家、下跌 {_num(market.get('down_count'))} 家。")
    return points


def _quality_banner(manifest: Mapping[str, Any], report: Mapping[str, Any]) -> tuple[str, str, str]:
    """Build the first email block from persisted manifest/report facts only."""
    sources = manifest.get("sources") if isinstance(manifest.get("sources"), Mapping) else {}
    slices = manifest.get("slices") if isinstance(manifest.get("slices"), Mapping) else {}
    report_manifest = report.get("manifest") if isinstance(report.get("manifest"), Mapping) else {}
    input_manifest = report_manifest.get("inputs") if isinstance(report_manifest.get("inputs"), Mapping) else {}
    market_volume = (report.get("chapters", {}).get("market_price_volume", {})
                     if isinstance(report.get("chapters"), Mapping) else {})
    baseline_comparison = (market_volume.get("auction_baseline_comparison", {})
                           if isinstance(market_volume, Mapping) else {})
    if not isinstance(baseline_comparison, Mapping):
        baseline_comparison = {}
    datasets: list[str] = []
    labels = {"auction_baseline": "竞价基线", "plate_members": "板块成员",
              "hot_rank": "热榜", "hot_plates_today": "当日热板", "hot_plates_yesterday": "昨日热板",
              "q2_redis": "Q2", "kaipan_relation": "板块关系", "td": "TD",
              "daily_history": "日K/指标", "daily_report_features": "日K/指标",
              "daily_kline": "日K", "unavailable_slice": "unavailable_slice"}
    def source_entry(key: str, value: Mapping[str, Any], *, is_slice: bool) -> str:
        state = value.get("status")
        if value.get("provided") is False:
            state = "NOT_ENABLED"
        if state is None and value.get("available") is not None:
            state = "READY" if value.get("available") else "MISSING"
        state = str(state or "UNKNOWN")
        count = value.get("rows")
        if count is None:
            count = value.get("coverage")
        detail = f"{count}行" if isinstance(count, (int, float)) else ""
        if isinstance(count, str):
            detail = count
        if key == "auction_baseline":
            coverage_value = value.get("coverage") if isinstance(value.get("coverage"), Mapping) else {}
            input_value = input_manifest.get(key) if isinstance(input_manifest.get(key), Mapping) else {}
            sample = coverage_value.get("sample_days", value.get("sample_days",
                     input_value.get("sample_days", baseline_comparison.get("sample_days"))))
            expected = coverage_value.get("expected_days", 64)
            if sample is not None:
                detail = f"样本 {sample}/{expected} 日" + (f"；{detail}" if detail else "")
            elif state in {"READY", "PARTIAL"}:
                detail = f"{detail}；样本天数 UNKNOWN（切片未提供 coverage）" if detail else "样本天数 UNKNOWN（切片未提供 coverage）"
        if key == "plate_members":
            registry = slices.get("plate_registry")
            if isinstance(registry, Mapping) and isinstance(registry.get("rows"), (int, float)):
                detail = "；".join(part for part in (detail, f"{registry['rows']}板块") if part)
        reason = value.get("reason")
        if reason and state in {"UNKNOWN", "MISSING", "PARTIAL", "DEGRADED"}:
            detail = f"{detail}（{reason}）" if detail else f"（{reason}）"
        source = value.get("version_source") or value.get("source")
        version = value.get("version")
        if source or version:
            detail = "；".join(part for part in (detail, f"来源 {source}" if source else None,
                                                f"版本 {version}" if version else None) if part)
        if not is_slice and not detail and state == "UNKNOWN":
            detail = "状态缺项"
        separator = "" if detail.startswith("（") else " "
        return f"{labels.get(key, key)}：{state}" + (f"{separator}{detail}" if detail else "")

    chosen: dict[str, Mapping[str, Any]] = {str(k): v for k, v in sources.items() if isinstance(v, Mapping)}
    chosen.update({str(k): v for k, v in slices.items() if isinstance(v, Mapping)})
    ready_daily_alias = any(
        key in chosen and str(chosen[key].get("status") or "").upper() == "READY"
        for key in ("daily_history", "daily_report_features"))
    for key, value in chosen.items():
        if not isinstance(value, Mapping):
            continue
        if key == "daily_kline" and ready_daily_alias:
            continue
        datasets.append(source_entry(key, value, is_slice=key in slices))
    for key, value in input_manifest.items():
        if key not in chosen and isinstance(value, Mapping):
            if value.get("provided") is False:
                datasets.append(f"{labels.get(str(key), str(key))}：NOT_ENABLED（未启用）")
            else:
                datasets.append(f"{labels.get(str(key), str(key))}：UNKNOWN（来源未进入 report manifest）")
    daily_sources = ("daily_history", "daily_report_features", "daily_kline")
    present_daily = [key for key in daily_sources if key in chosen]
    if not present_daily:
        datasets.append("日K/指标：UNKNOWN（无切片记录）")
    elif not any(str(chosen[key].get("status") or "").upper() == "READY"
                 for key in present_daily):
        # The banner describes the usable daily slice, not an obsolete alias
        # that may still be present in manifests from older report versions.
        daily = chosen[present_daily[0]]
        datasets.append(source_entry("daily_history", daily, is_slice=True))
    if "plate_members" not in chosen:
        datasets.append("板块成员：UNKNOWN（无切片记录）")
    quality = report.get("data_quality") if isinstance(report.get("data_quality"), Mapping) else {}
    for key, value in quality.items():
        if isinstance(value, Mapping) and value.get("status") and key not in sources:
            datasets.append(f"{labels.get(str(key), str(key))}：{value['status']}")
    degraded = manifest.get("degraded") or []
    if not isinstance(degraded, list):
        degraded = [degraded]
    states = []
    for value in chosen.values():
        if isinstance(value, Mapping):
            state = value.get("status")
            if state is None and value.get("available") is not None:
                state = "READY" if value.get("available") else "MISSING"
            states.append(str(state or ""))
    if any(state in {"MISSING", "PARTIAL"} for state in states):
        status = "PARTIAL"
    elif degraded or any(state == "DEGRADED" for state in states):
        status = "DEGRADED"
    elif states and all(state == "READY" for state in states):
        status = "READY"
    else:
        status = "UNKNOWN"
    used_dates = manifest.get("used_dates") if isinstance(manifest.get("used_dates"), Mapping) else {}
    used_date = manifest.get("used_date") or report.get("used_date")
    if used_date is None and used_dates:
        used_date = used_dates
    trade_date = str(manifest.get("trade_date") or report.get("trade_date") or "—")
    used_values = sorted({str(day) for dates in used_dates.values()
                          for day in (dates if isinstance(dates, (list, tuple, set)) else [dates])
                          if day})
    date_note = "；用 D-1 数据" if used_values and any(day < trade_date for day in used_values) else ""
    visible_dates = {key: value for key, value in used_dates.items() if key != "trading_calendar"}
    if isinstance(visible_dates, Mapping) and visible_dates:
        def compact_dates(value):
            days = sorted(map(str, value if isinstance(value, (list, tuple, set)) else [value]))
            if len(days) > 3:
                return f"{days[0]}..{days[-1]}（{len(days)}日）"
            return ",".join(days)
        used_text = "；".join(f"{key}={compact_dates(value)}" for key, value in visible_dates.items())
    elif isinstance(used_date, Mapping):
        used_text = "；".join(f"{key}={value}" for key, value in used_date.items())
    else:
        used_text = str(used_date) if used_date is not None else ""
    elapsed = manifest.get("elapsed_seconds")
    lines = [f"数据完整度与异常 [{status}]",
             "输入数据集：" + ("；".join(datasets) if datasets else "UNKNOWN（无数据集状态记录）"),
             f"耗时：{elapsed}s" if elapsed is not None else "耗时：—",
             (f"used_date：{used_text}{date_note}" if used_text else
              f"used_date：UNKNOWN（报告交易日 {trade_date}）[manifest 未记录实际使用日期]"),
             "降级标记：" + ("；".join(map(str, degraded)) if degraded else "无")]
    anomalies = []
    def walk_numbers(value: Any, path: str = ""):
        if isinstance(value, Mapping):
            for key, child in value.items():
                yield from walk_numbers(child, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            return
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            yield path, value
    for name, value in walk_numbers(report):
        if any(token in name.lower() for token in ("multiple", "倍数")) and (value > 20 or value < 0.05):
            anomalies.append(f"{name}={value}（倍数超出 0.05–20）")
    for key, source in sources.items():
        if not isinstance(source, Mapping) or not isinstance(source.get("coverage"), Mapping):
            continue
        coverage = source["coverage"]
        numerator = next((coverage.get(name) for name in ("priced_symbols", "matched", "available")
                          if isinstance(coverage.get(name), (int, float))), None)
        denominator = next((coverage.get(name) for name in ("symbols", "expected", "total")
                            if isinstance(coverage.get(name), (int, float)) and coverage.get(name) > 0), None)
        if numerator is not None and denominator is not None and numerator / denominator < 0.8:
            anomalies.append(f"{key} 缺失率 {1 - numerator / denominator:.1%}（超过 20%）")
    lines.append("疑似异常：" + ("；".join(anomalies[:8]) if anomalies else "未检出已配置数值异常"))
    plain = "\n".join(lines)
    html_block = '<section style="background:#fff4d6;padding:12px 14px;margin-bottom:14px"><strong>' + "</strong><br>".join(_esc(line) for line in lines) + "</section>"
    return status, plain, html_block


def _metrics(report: Mapping[str, Any], stage: str) -> list[tuple[str, Any, str]]:
    if stage == "close" or "market" in report:
        market = report.get("market") if isinstance(report.get("market"), dict) else {}
        candidates = [("全天成交额", market.get("amount_yuan"), "amount_yuan"),
                      (f"对前日倍数（固定共同池 {_plain_value(market.get('amount_common_pool_n'))} 只）", market.get("amount_vs_prev_common"), "vs_amount"),
                      (f"对64日中位数（固定共同池 {_plain_value(market.get('amount_64d_common_pool_n'))} 只）", market.get("amount_vs_64d_median_common"), "vs_amount"),
                      (f"64日历史分位（固定共同池 {_plain_value(market.get('amount_64d_common_pool_n'))} 只）", market.get("amount_64d_percentile_common"), "rate"),
                      ("收盘中位涨幅", market.get("close_median"), "close_median"),
                      ("上涨家数", market.get("up_count"), "count"),
                      ("下跌家数", market.get("down_count"), "count"),
                      ("收盘涨停价代理 / 跌停价代理", f"{_plain_value(market.get('close_limit_up_count'))} / {_plain_value(market.get('close_limit_down_count'))}", "")]
        quality = report.get("data_quality") if isinstance(report.get("data_quality"), Mapping) else {}
        if market.get("amount_vs_prev") is not None or market.get("amount_vs_64d_median") is not None:
            candidates.append(("全市场口径注脚", f"较前日 {_num(market.get('amount_vs_prev'), 'vs_amount')}；64日中位 {_num(market.get('amount_vs_64d_median'), 'vs_amount')}", ""))
        coverage = {"日线覆盖": f"{_plain_value(quality.get('close_pct_coverage'))}/{_plain_value(quality.get('included_stock_count'))}",
                    "0925覆盖": quality.get("auction_pct_coverage"), "0932覆盖": quality.get("p0932_coverage"),
                    "0945覆盖": quality.get("p0945_coverage"),
                    "关系快照日期": quality.get("relation_snapshot_date"),
                    "关系滞后交易日": quality.get("relation_lag_trading_days")}
        checkpoints = quality.get("checkpoint_coverage")
        if isinstance(checkpoints, Mapping):
            coverage["检查点覆盖"] = "、".join(f"{key} {_num(value, 'rate')}" for key, value in checkpoints.items())
        populated = {key: value for key, value in coverage.items() if value is not None}
        if populated:
            candidates.append(("数据覆盖", "；".join(f"{key} {value}" for key, value in populated.items()), ""))
        if quality.get("final_count") is not None or quality.get("indicative_count") is not None:
            candidates.append(("价格可得性", f"收盘对账价覆盖 {_plain_value(quality.get('close_pct_coverage'))} 只；FINAL {_plain_value(quality.get('final_count'))} 只；INDICATIVE {_plain_value(quality.get('indicative_count'))} 只；首次达到时刻 UNKNOWN", ""))
    else:
        terminal = report.get("auction_terminal_0925") if isinstance(report.get("auction_terminal_0925"), dict) else {}
        if not terminal and isinstance(report.get("handoff"), dict):
            handoff_terminal = report["handoff"].get("auction_0925", {})
            terminal = handoff_terminal if isinstance(handoff_terminal, dict) else {}
        history = report.get("history") if isinstance(report.get("history"), dict) else {}
        candidates = [("竞价 / 累计成交额", terminal.get("market_match_amount_yuan"), "amount_yuan"),
                     ("对前日 / 基线倍数", terminal.get("vs_amount_baseline", terminal.get("vs_previous_day_final")), "vs_amount"),
                     ("成交额加权涨幅", terminal.get("amount_weighted_pct_change"), "pct"),
                     ("开盘中位涨幅", terminal.get("opening_median_pct_change", history.get("current_median_open_pct")), "pct"),
                     ("上涨家数占比", terminal.get("up_share_pct", history.get("current_up_share_pct")), "pct"),
                     ("涨停 / 跌停", f"{_plain_value(terminal.get('sealed_up_count'))} / {_plain_value(terminal.get('sealed_down_count'))}", "")]
        volume = report.get("chapters", {}).get("market_price_volume", {}) if isinstance(report.get("chapters"), Mapping) else {}
        if isinstance(volume, Mapping):
            volume_valid = (volume.get("status") not in {"STALE", "NOT_READY", "NOT_YET_AVAILABLE"}
                            and volume.get("symbol_count") != 0)
            candidates += [("数据时点", volume.get("asof"), ""), ("数据滞后秒", volume.get("data_lag_seconds"), ""),
                           ("有价标的数", volume.get("priced_symbol_count") if volume_valid else None, "count")]
    return [(label, value, key) for label, value, key in candidates if value is not None]


def _chapters(report: Mapping[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    chapters = report.get("chapters") if isinstance(report.get("chapters"), dict) else {}
    out = []
    replaced_keys = set()
    if isinstance(report.get("a_to_b"), list):
        replaced_keys.add("look_a_do_b")
    if isinstance(report.get("ladder_section"), Mapping):
        replaced_keys.update({"yesterday_highboards", "ladder_summary"})
    if isinstance(report.get("stocks"), list):
        replaced_keys.add("market_price_volume")
    for key, value in chapters.items():
        if key in replaced_keys:
            continue
        label = _label(key)
        if label and isinstance(value, list) and value and isinstance(value[0], dict):
            rows = value[:12]
            if key in {"big_meat", "big_loss", "extremes"}:
                keep = ("symbol", "name", "main_plate", "limit_pct", "auction_pct", "p0925_pct", "p0932_pct",
                        "p0945_pct", "open_pct", "close_pct", "high_pct", "low_pct", "delta_pp", "delta_over_limit",
                        "auction_amount_yuan", "auction_amount_bp", "in_yest_pool", "lb_days", "path_flags",
                        "visibility", "visibility_half_limit", "auction_price_status")
                rows = [{field: row[field] for field in keep if field in row}
                        for row in rows if isinstance(row, Mapping)]
            out.append((label, rows))
    changes = chapters.get("plate_changes")
    if isinstance(changes, Mapping):
        rows = []
        for direction, key in (("上涨", "up"), ("下跌", "down")):
            vals = changes.get(key)
            if isinstance(vals, list):
                rows.extend(dict(row, status=direction) for row in vals[:5] if isinstance(row, dict))
        if rows:
            out.append(("板块变化", rows))
    linkage = chapters.get("plate_linkage")
    if isinstance(linkage, Mapping) and isinstance(linkage.get("rows"), list) and linkage["rows"]:
        out.append(("个股—板块联动", linkage["rows"][:10]))
    market_volume = chapters.get("market_price_volume")
    if isinstance(market_volume, Mapping):
        summary_fields = ("market_match_amount_yuan", "vs_previous_day_final", "vs_amount_baseline",
                          "amount_weighted_pct_change", "median_pct_change", "up_share_pct",
                          "down_amount_share_pct", "top100_concentration_pct", "data_lag_seconds")
        summary = {key: market_volume[key] for key in summary_fields if key in market_volume and market_volume[key] is not None}
        baseline = market_volume.get("auction_baseline_comparison")
        if isinstance(baseline, Mapping):
            for source_key, target_key in (("sample_days", "amount_64d_sample_days"),
                                           ("median_yi", "median_yi"),
                                           ("multiple", "multiple"), ("percentile", "percentile")):
                if baseline.get(source_key) is not None:
                    summary[target_key] = baseline[source_key]
            summary["pool_label"] = "TD全市场可用竞价池；历史基线64交易日"
        if summary:
            out.append(("大盘竞价/开盘量价", [summary]))
        top_plates = market_volume.get("top_plates_by_share")
        if isinstance(top_plates, list) and top_plates and not any(title.startswith("板块竞价额") for title, _ in out):
            out.append(("板块竞价额份额与涨幅（成员重叠，不可相加）", top_plates[:10]))
        top_stocks = market_volume.get("top_stocks_by_amount")
        if isinstance(top_stocks, list) and top_stocks and not isinstance(report.get("stocks"), list):
            out.append(("个股竞价额前二十（含前十）", top_stocks[:20]))
    look = chapters.get("look_a_do_b")
    if not isinstance(report.get("a_to_b"), list) and isinstance(look, list) and look:
        out.append(("看A做B", look[:10]))
    if not isinstance(report.get("a_to_b"), list) and isinstance(chapters.get("look_a_do_b"), list) and not chapters["look_a_do_b"]:
        out.append(("看A做B", [{"status": "当前报告无匹配样本"}]))
    if "market" in report:
        market = report.get("market") if isinstance(report.get("market"), Mapping) else {}
        stock_rows = report.get("stocks") if isinstance(report.get("stocks"), list) else []
        path_row = {}
        for key, column in (("p0925_mean", "p0925_pct"), ("p0932_mean", "p0932_pct"),
                            ("p0945_mean", "p0945_pct"), ("close_mean", "close_pct")):
            values = [row.get(column) for row in stock_rows if isinstance(row, Mapping)
                      and isinstance(row.get(column), (int, float))]
            if values:
                path_row[key] = sum(values) / len(values)
        if path_row:
            path_row["coverage_note"] = "各时点均值独立排除缺值；n_* 为该时点有值数"
            for key, column in (("p0925_mean", "p0925_pct"), ("p0932_mean", "p0932_pct"),
                                ("p0945_mean", "p0945_pct"), ("close_mean", "close_pct")):
                path_row[key.replace("_mean", "_n")] = sum(
                    1 for row in stock_rows if isinstance(row, Mapping)
                    and isinstance(row.get(column), (int, float)))
            out.append(("市场路径（各时点有值样本均值）", [path_row]))
        metric_keys = ("amount_64d_median_yuan", "amount_vs_64d_median_common",
                       "amount_64d_percentile_common", "amount_64d_common_pool_n",
                       "amount_64d_common_pool_days", "amount_64d_sample_days", "auction_regime",
                       "auction_median", "p0945_median", "close_median", "up_count", "down_count",
                       "close_limit_up_count", "close_limit_down_count")
        metric_row = {key: market[key] for key in metric_keys if market.get(key) is not None}
        if metric_row:
            pool_n = market.get("amount_64d_common_pool_n")
            metric_row["pool"] = (f"全天成交额；当日及64日历史共同池（{int(pool_n):,}只）"
                                   if isinstance(pool_n, (int, float)) else
                                   "全天成交额；当日及64日历史共同池（标的数缺失）")
            out.append(("全天成交额历史基线（共同池）", [metric_row]))
        comparison = market.get("auction_baseline_comparison")
        if isinstance(comparison, Mapping):
            auction_row = {"auction_amount_yuan": market.get("auction_amount_yuan"),
                           "median_yi": comparison.get("median_yi"),
                           "auction_baseline_multiple": comparison.get("multiple"),
                           "auction_baseline_percentile": comparison.get("percentile"),
                           "sample_days": comparison.get("sample_days"),
                           "pool_label": "各日竞价额总和；阈值/覆盖按 auction_amount_by_day.csv"}
            out.append(("竞价额历史基线（竞价池）", [auction_row]))
        elif market.get("auction_baseline_status") == "MISSING":
            out.append(("竞价额历史基线（竞价池）", [{"status": "缺口：未读取竞价额自身历史基线"}]))
        stocks = report.get("stocks")
        if isinstance(stocks, list):
            ranked = sorted((row for row in stocks if isinstance(row, Mapping)
                             and isinstance(row.get("auction_amount_yuan"), (int, float))),
                            key=lambda row: (-row["auction_amount_yuan"], str(row.get("symbol", ""))))[:20]
            if ranked:
                total = sum((row.get("auction_amount_yuan") or 0) for row in stocks
                            if isinstance(row, Mapping) and isinstance(row.get("auction_amount_yuan"), (int, float)))
                amount_rows = []
                for rank_no, source in enumerate(ranked, 1):
                    row = dict(source, rank=rank_no)
                    row["amount_share_bp"] = (source["auction_amount_yuan"] / total * 10000) if total else None
                    row["auction_price_status"] = source.get("auction_price_status")
                    amount_rows.append(row)
                out.append(("个股竞价额前二十（前十为同表前十行）", amount_rows))
        extreme_rows = []
        for key in ("big_meat", "big_loss"):
            val = report.get(key)
            if isinstance(val, list) and val and isinstance(val[0], dict):
                keep = ("symbol", "name", "main_plate", "limit_pct", "auction_pct", "p0925_pct", "p0932_pct",
                        "p0945_pct", "open_pct", "close_pct", "high_pct", "low_pct", "delta_pp", "delta_over_limit",
                        "auction_amount_yuan", "auction_amount_bp", "in_yest_pool", "lb_days", "path_flags",
                        "visibility", "visibility_half_limit", "auction_price_status")
                extreme_rows.extend(dict({field: row[field] for field in keep if field in row},
                                         result_type="大肉" if key == "big_meat" else "大面")
                                    for row in val[:4] if isinstance(row, Mapping))
        if extreme_rows:
            out.append(("极值与首次观测", extreme_rows))
        for key, title in (("plate_evolution", "个股—板块演化（事后选出，非早盘已知B候选）"), ("ladder_summary", "连板梯队")):
            val = report.get(key)
            if isinstance(val, list) and val and isinstance(val[0], dict):
                out.append((title, val[:8] if key == "plate_evolution" else val[:12]))
        meat_rows = report.get("big_meat") if isinstance(report.get("big_meat"), list) else []
        exceptional_rows, ordinary_meat = [], []
        for row in meat_rows:
            band, close = row.get("limit_pct"), row.get("close_pct")
            if isinstance(band, (int, float)) and isinstance(close, (int, float)) and band > 0 and abs(close) > band * 1.5:
                exceptional_rows.append(dict(row, price_band_label="新股/无涨跌幅限制（超出标称板幅，规则推定）"))
            else:
                ordinary_meat.append(row)
        if exceptional_rows:
            out = [(title, ordinary_meat[:10] if title == "大肉" else rows) for title, rows in out]
            for title, rows in out:
                if title == "极值与首次观测":
                    rows.extend(dict(row, result_type="新股/无涨跌幅限制") for row in exceptional_rows[:4])
                    break
        plates = report.get("plates")
        unlimited = report.get("unlimited_price_band_stocks")
        if isinstance(unlimited, list) and unlimited:
            if extreme_rows:
                extreme_rows.extend(dict(row, result_type="新股/无涨跌幅限制") for row in unlimited[:4])
            else:
                out.append(("极值与首次观测", [dict(row, result_type="新股/无涨跌幅限制") for row in unlimited[:4]]))
        if isinstance(plates, dict):
            close_extremes = []
            for key in ("close_top10", "close_bottom10"):
                val = plates.get(key)
                if isinstance(val, list):
                    close_extremes.extend(dict(row, direction="收盘强势" if key == "close_top10" else "收盘弱势")
                                          for row in val[:10] if isinstance(row, Mapping))
            if close_extremes:
                out.append(("收盘板块相对涨幅（强弱合并）", close_extremes))
            for key in ("auction_amount_top10",):
                val = plates.get(key)
                if isinstance(val, list) and val and not any(title.startswith("竞价额前十") for title, _ in out):
                    title = FIELD_LABELS[key]
                    if key == "auction_amount_top10":
                        title += "（成员重叠，不可相加）"
                    out.append((title, val[:10]))
        scorecard = report.get("emerging_scorecard")
        if isinstance(scorecard, Mapping):
            rows = [{"cohort": label, **scorecard[key]} for key, label in (
                ("emerging", "严格新冒头"), ("p0945_top_k", "0945涨幅前K"), ("amount_top_k", "0945累计额前K"))
                    if isinstance(scorecard.get(key), Mapping)]
            if rows:
                out.append(("新冒头与同口径对照", rows))
        yesterday = report.get("yesterday_review")
        if isinstance(yesterday, Mapping) and yesterday.get("status") == "UNAVAILABLE_NO_PREV_REPORT":
            out.append(("昨日机会回看", [{"status": "无昨日报告"}]))
        elif not isinstance(yesterday, Mapping):
            out.append(("昨日机会回看", [{"status": "无昨日报告"}]))
    terminal = report.get("auction_terminal_0925") if isinstance(report.get("auction_terminal_0925"), Mapping) else {}
    top_stocks = terminal.get("top_stocks_by_amount") if isinstance(terminal, Mapping) else None
    if isinstance(top_stocks, list) and top_stocks and not isinstance(report.get("stocks"), list):
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
    if isinstance(top_plates, list) and top_plates and not any(title.startswith("板块竞价额") for title, _ in out):
        out.append(("板块竞价额占比", top_plates[:10]))
    editorial = report.get("email_editorial")
    if isinstance(editorial, dict):
        for title, rows in editorial.items():
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                out.append(("收盘核心对照" if title == "SCORECARD" else title, rows[:20]))
    if isinstance(report.get("a_to_b"), list):
        ab_rows = []
        for group in report["a_to_b"]:
            if not isinstance(group, Mapping):
                continue
            peers = group.get("candidates") if isinstance(group.get("candidates"), list) else []
            if not peers:
                ab_rows.append({"a_symbol": group.get("a_symbol"), "a_name": group.get("a_name"),
                                "plate": group.get("plate"), "reason": group.get("reason")})
            for peer in peers:
                ab_rows.append({"a_symbol": group.get("a_symbol"), "a_name": group.get("a_name"),
                                "plate": group.get("plate"), "asof": group.get("asof"),
                                "relation_asof": group.get("relation_asof"), **peer})
        if ab_rows:
            total_candidates = len(ab_rows)
            ab_rows = ab_rows[:5]
            out.append((f"看A做B候选（早盘可见定义；前5项/共{total_candidates}项）", ab_rows))
        else:
            out.append(("看A做B候选（早盘可见定义）", [{"status": report.get("a_to_b_status") or "无A或未生成"}]))
    ladder = report.get("ladder_section")
    if isinstance(ladder, Mapping):
        summary = ladder.get("summary") if isinstance(ladder.get("summary"), list) else []
        summary_head = summary[0] if summary and isinstance(summary[0], Mapping) else {}
        ladder_rows = [{"count_label": ladder.get("count_label"),
                        "today_promotion_or_price_proxy_count": ladder.get("today_promotion_or_price_proxy_count"),
                        "emotion_asof": ladder.get("emotion_asof"),
                        "postmarket_label": ladder.get("postmarket_label"),
                        "emotion_label": (ladder.get("emotion") or {}).get("label"),
                        "coverage_0945": (ladder.get("emotion") or {}).get("coverage_0945"),
                        **{key: summary_head.get(key) for key in ("max_price_streak", "first_board_count",
                            "previous_limit_n", "promoted_n", "broken_n", "promotion_rate",
                            "previous_space_height") if summary_head.get(key) is not None}}]
        for level in ladder.get("yesterday_board_counts", []):
            if isinstance(level, Mapping):
                ladder_rows.append(dict(level))
        for level in summary[1:]:
            if isinstance(level, Mapping):
                ladder_rows.append(dict(level))
        for row in ladder.get("rows", []):
            if isinstance(row, Mapping):
                ladder_rows.append(dict(row))
        out.append(("连板梯队与路径", ladder_rows or [{"status": "无可展示梯队行"}]))
    feedback = report.get("streak_feedback")
    if isinstance(feedback, Mapping):
        checkpoint = "0925" if str(report.get("stage", "")).lower().startswith("auction") else "0945"
        point = feedback.get("checkpoints", {}).get(checkpoint, {})
        matrix = point.get("matrix", {}) if isinstance(point, Mapping) else {}
        detail_rows = []
        for level, counts in matrix.get("by_level", {}).items() if isinstance(matrix, Mapping) else ():
            detail_rows.append({"维度": "板高", "名称": level, **dict(counts), "状态": "竞价暂时" if point.get("provisional") else "截点状态"})
        themes = matrix.get("by_theme", {}) if isinstance(matrix, Mapping) else {}
        for key, counts in sorted(themes.items(), key=lambda item: (-item[1].get("total", 0), item[0]))[:10]:
            detail_rows.append({"维度": "主主题", "名称": counts.get("theme", key), **dict(counts), "状态": "竞价暂时" if point.get("provisional") else "截点状态"})
        overall = matrix.get("overall", {}) if isinstance(matrix, Mapping) else {}
        if overall:
            detail_rows.insert(0, {"维度": "昨日连板整体", "名称": "涨幅", "mean_return_pct": overall.get("mean_return_pct"),
                                   "median_return_pct": overall.get("median_return_pct"), "red_share": overall.get("red_share"),
                                   "return_count": overall.get("return_count"), "unknown": overall.get("unknown")})
        out.append(("昨日连板今日表现", detail_rows or [{"status": "昨日连板池或截点字段缺失；状态 UNKNOWN"}]))
    priority = {"市场量价与历史位置": 0, "全天成交额历史基线（共同池）": 0,
                "竞价额历史基线（竞价池）": 0, "大盘竞价/开盘量价": 0, "数据质量": 1,
                "市场路径": 2, "市场路径（各时点有值样本均值）": 2,
                "连板梯队与路径": 3, "看A做B候选（前5项）": 4,
                "竞价额前列个股": 3, "个股竞价额前列": 3, "个股竞价额前二十（含前十）": 5,
                "个股竞价额前二十（前十为同表前十行）": 5,
                "板块竞价额占比": 6, "板块竞价额份额与涨幅（成员重叠，不可相加）": 6,
                "竞价额前十（成员重叠，不可相加）": 6,
                "个股—板块演化（事后选出，非早盘已知B候选）": 7,
                "收盘板块相对涨幅（强弱合并）": 7, "极值与首次观测": 8,
                "昨日机会回看": 9, "收盘核心对照": 10}
    return sorted(out, key=lambda item: (priority.get(item[0],
        4 if item[0].startswith("看A做B候选") else 9), item[0]))[:15]


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
            if not isinstance(terminal, Mapping):
                terminal = {}
        lag = td.get("data_lag_seconds", terminal.get("data_lag_seconds"))
        coverage = td.get("coverage") if isinstance(td.get("coverage"), Mapping) else {}
        volume = report.get("chapters", {}).get("market_price_volume", {}) if isinstance(report.get("chapters"), Mapping) else {}
        volume_valid = (not isinstance(volume, Mapping) or
                        (volume.get("status") not in {"STALE", "NOT_READY", "NOT_YET_AVAILABLE"}
                         and volume.get("symbol_count") != 0))
        if lag is not None:
            lines.append(f"数据滞后：{_num(lag)} 秒")
        if not volume_valid:
            lines.append("TD 截面不可用；覆盖未知")
        elif coverage.get("symbols") is not None:
            lines.append(f"覆盖：{_num(coverage.get('priced_symbols'))} / {_num(coverage['symbols'])} 只")
    return lines


def render_email_html(report_json: Mapping[str, Any] | str | bytes, stage: str,
                      trade_date: str, manifest: Mapping[str, Any] | None = None, *,
                      full_report: bool = False) -> tuple[str, str]:
    """Render report JSON into compact HTML and readable plain text."""
    report = json.loads(report_json) if isinstance(report_json, (str, bytes)) else report_json
    if not isinstance(report, Mapping):
        raise ValueError("report JSON must be an object")
    stage_key = str(stage or report.get("stage") or "").lower()
    if "close" not in stage_key and "closing" not in stage_key and stage_key != "eod":
        forbidden = {"close_pct", "close_result_pct", "close_mean", "close_median",
                     "close_at_limit_up", "close_limit_up_count", "close_limit_down_count",
                     "close_ge_5", "close_ge_5_rate", "close_top10", "close_bottom10",
                     "close_strength_top10", "close_weakness_top10", "emerging_scorecard",
                     "yesterday_review"}
        if "auction" in stage_key:
            forbidden |= {"p0932_pct", "p0945_pct", "p0932_mean", "p0945_mean",
                          "p0932_median", "p0945_median", "p0932_n", "p0945_n",
                          "relative_to_plate_pp", "p0932", "p0945"}
        auction_only = "auction" in stage_key
        def strip_close(value):
            if isinstance(value, Mapping):
                cleaned = {}
                for key, item in value.items():
                    if key in forbidden:
                        continue
                    if auction_only and key in {"0932", "0945", "close"}:
                        continue
                    if auction_only and key in {"equal_weight_0945_pct", "equal_weight_close_pct",
                                                "relative_to_plate_0945_pp", "relative_to_plate_close_pp"}:
                        continue
                    if not auction_only and key == "close":
                        continue
                    cleaned[key] = strip_close(item)
                return cleaned
            if isinstance(value, list):
                return [strip_close(item) for item in value]
            return value
        report = strip_close(report)
    manifest = manifest or (report.get("manifest") if isinstance(report.get("manifest"), Mapping) else {})
    panorama = report.get("auction_panorama")
    if "auction" in stage_key:
        quickbrief = (panorama.get("quickbrief") if isinstance(panorama, Mapping) else None)
        if not isinstance(quickbrief, Mapping):
            quickbrief = report.get("auction_quickbrief")
        public_lines = quickbrief.get("email_lines") if isinstance(quickbrief, Mapping) else None
        if isinstance(public_lines, list) and public_lines and not full_report:
            status, _, _ = _quality_banner(manifest, report)
            if isinstance(panorama, Mapping):
                from .auction_panorama_view import render_auction_email_digest
                degraded_note = ""
                if status != "READY":
                    for group_name in ("slices", "sources"):
                        group = manifest.get(group_name) if isinstance(manifest, Mapping) else None
                        if not isinstance(group, Mapping):
                            continue
                        issue = next(((str(name), value) for name, value in group.items()
                                      if isinstance(value, Mapping)
                                      and str(value.get("status", "UNKNOWN")).upper() != "READY"), None)
                        if issue:
                            name, detail = issue
                            reason = detail.get("reason")
                            degraded_note = f"原因：{name} {detail.get('status', 'UNKNOWN')}"
                            if reason:
                                degraded_note += f" [{v6_failure_public_code(reason)}]"
                            break
                    if not degraded_note:
                        degraded_note = "原因：数据覆盖或来源状态未完整"
                return render_auction_email_digest(panorama, quickbrief,
                                                   completeness=status,
                                                   degraded_note=degraded_note)
            warning = status != "READY"
            title = str(public_lines[0])
            replay_note = (str(public_lines[1]) if len(public_lines) > 1 and
                           str(public_lines[1]).startswith("历史回放样本") else None)
            content_lines = public_lines[2:] if replay_note else public_lines[1:]
            public_html = [f'<h1 style="font-size:18px;margin:0 0 3px">{_esc(title)}</h1>']
            if replay_note:
                public_html.append(f'<div style="font-size:11px;color:#667085;margin-bottom:10px">{_esc(replay_note)}</div>')
            if warning:
                public_html.append(f'<div style="font-size:11px;color:#7c2d12;margin:5px 0">数据完整度与异常 [{_esc(status)}]</div>')
            failure = report.get("v6_panorama_failure")
            failure_code = v6_failure_public_code(failure) if failure else ""
            if failure_code:
                public_html.append(f'<div role="alert" style="font-size:11px;color:#7c2d12;margin:5px 0">V6 全景缺失：{_esc(failure_code)}</div>')
            for line in content_lines:
                label = line if line.endswith(":") else None
                public_html.append((f'<div style="font-weight:700;margin-top:8px">{_esc(label)}</div>'
                                    if label else f'<div style="line-height:1.55">{_esc(str(line))}</div>'))
            plain = "\n".join(str(line) for line in public_lines)
            if warning:
                plain = plain.replace("\n", f"\n数据完整度与异常 [{status}]\n", 1)
            if failure_code:
                plain = f"V6 全景缺失：{failure_code}\n" + plain
            plain += "\n完整明细已存档于 Core 报告目录"
            public_html.append('<div style="margin-top:10px;font-size:11px;color:#667085">完整明细已存档于 Core 报告目录</div>')
            return (('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head>'
                     '<body style="font-family:-apple-system,BlinkMacSystemFont,\'PingFang SC\',sans-serif;color:#26364a">'
                     '<main style="max-width:680px;margin:auto;padding:12px">' + ''.join(public_html) +
                     '</main></body></html>'), plain)
        if isinstance(panorama, Mapping):
            from .auction_panorama_view import render_auction_panorama
            full_html, full_plain = render_auction_panorama(panorama)
            if full_report:
                _quality_status, quality_plain, quality_html = _quality_banner(manifest, report)
                quality_section = ('<section style="margin:14px auto;padding:12px;background:#fff;padding:12px;'
                                   'max-width:760px"><h2 style="font-size:15px">数据完整度与异常摘要</h2>'
                                   + quality_html + '</section>')
                full_html = full_html.replace('</body>', quality_section + '</body>', 1)
                full_plain += "\n\n" + quality_plain
                return full_html, full_plain
            warning = '<div role="alert" style="padding:12px;background:#fff0e5;border:2px solid #c2410c;font-weight:700">竞价速览构建失败,以下为完整报告回退。</div>'
            return (full_html.replace('</body>', warning + '</body>', 1),
                    "竞价速览构建失败,以下为完整报告回退。\n\n" + full_plain)
    quality_status, quality_plain, quality_html = _quality_banner(manifest, report)
    failure = report.get("v6_panorama_failure")
    failure_code = v6_failure_public_code(failure) if failure else ""
    warning = (f'<div role="alert" style="margin:10px;padding:12px;background:#fff0e5;border:2px solid #c2410c;color:#7c2d12;font-weight:700">'
               f'V6 全景缺失：{_esc(failure_code)}</div>' if "auction" in stage_key and failure else "")
    warning_text = f"V6 全景缺失：{failure_code}\n\n" if "auction" in stage_key and failure else ""
    ladder = report.get("ladder_section")
    if (isinstance(report.get("stocks"), list)
            or (isinstance(ladder, Mapping) and bool(ladder.get("rows")))
            or report.get("email_layout") == "board"):
        board_html, board_plain = render_email_board(report, stage_key, trade_date)
        board_html = re.sub(r"(<body\b[^>]*>)", lambda match: match.group(1) + quality_html + warning, board_html, count=1)
        return (board_html,
                quality_plain + "\n\n" + warning_text + board_plain)
    stage_name = "收盘" if "close" in stage_key else "开盘" if "open" in stage_key else "竞价"
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
    gaps = []
    if editorial_gaps:
        gaps.extend(str(row.get("description")) for row in editorial_gaps
                    if isinstance(row, Mapping) and row.get("description"))
    for source in (report.get("limitations"), report.get("data_gaps"), report.get("degraded"),
                   manifest.get("degraded"), manifest.get("missing")):
        if isinstance(source, list):
            gaps.extend(str(item) for item in source if item)
        elif source:
            gaps.append(str(source))
    gaps = list(dict.fromkeys(gaps))
    generated_gaps = []
    if "a_to_b" not in report:
        generated_gaps.append("A→B 章节未生成/原因：report 中缺少 a_to_b 数据")
    if "ladder_section" not in report:
        generated_gaps.append("连板梯队章节未生成/原因：report 中缺少 ladder_section 数据")
    plate_data = report.get("plates") if isinstance(report.get("plates"), Mapping) else {}
    if plate_data.get("status") in {"UNAVAILABLE_NO_RELATION", "PARTIAL"}:
        generated_gaps.append("个股—板块关系缺失或不完整，相关演化与份额列不展示")
    chapters_data = report.get("chapters") if isinstance(report.get("chapters"), Mapping) else {}
    linkage_data = chapters_data.get("plate_linkage")
    if isinstance(linkage_data, Mapping) and linkage_data.get("available") is False:
        generated_gaps.append("当前报告未形成个股—板块联动样本")
    terminal_data = report.get("auction_terminal_0925")
    market_data = chapters_data.get("market_price_volume")
    has_plate_shares = (isinstance(terminal_data, Mapping) and bool(terminal_data.get("top_plates_by_share"))) or (
        isinstance(market_data, Mapping) and bool(market_data.get("top_plates_by_share")))
    if stage_key in {"auction", "open", "auction_0926", "open_0946"} and not has_plate_shares:
        generated_gaps.append("本时点没有可用的板块竞价额份额明细，未展示板块份额横条")
    if generated_gaps:
        gaps = list(gaps) if isinstance(gaps, list) else ([gaps] if gaps else [])
        gaps.extend(generated_gaps)
    if isinstance(gaps, list):
        gaps = [str(x) for x in gaps if x]
    hidden_columns = report.get("email_hidden_columns") if isinstance(report.get("email_hidden_columns"), list) else []
    if hidden_columns:
        gaps = list(gaps) if isinstance(gaps, list) else ([gaps] if gaps else [])
        gaps.append("整列无有效值，已隐藏：" + "、".join(map(str, hidden_columns)))
    gap_text = "；".join(_humanize_copy(gap) for gap in gaps[:8]) if isinstance(gaps, list) else _humanize_copy(gaps) if gaps else ""
    title = f"Core {stage_name}报告"
    status = None
    terminal = report.get("auction_terminal_0925") if isinstance(report.get("auction_terminal_0925"), Mapping) else {}
    if isinstance(report.get("handoff"), Mapping) and not terminal:
        handoff_terminal = report["handoff"].get("auction_0925", {})
        terminal = handoff_terminal if isinstance(handoff_terminal, Mapping) else {}
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
    auto_hidden_columns = []
    for chapter_title, rows in _chapters(report):
        keys = []
        for row in rows:
            for key in row:
                if key not in keys and _label(key):
                    keys.append(key)
        cols = [key for key in keys if any(row.get(key) is not None for row in rows)]
        if "main_plate" in cols and "plates" in cols:
            cols.remove("plates")
        if chapter_title in {"大肉", "大面"}:
            preferred = ("name", "main_plate", "limit_pct", "auction_pct", "auction_price_status", "p0932_pct",
                         "p0945_pct", "close_pct", "high_pct", "low_pct", "delta_over_limit",
                         "auction_amount_yuan", "auction_amount_bp", "in_yest_pool", "lb_days",
                         "visibility_half_limit")
            cols = [key for key in preferred if key in cols]
        elif chapter_title.startswith("看A做B候选"):
            preferred = ("plate", "name", "auction_pct",
                         "auction_amount_yuan", "auction_amount_bp", "prior_range_position20_pct",
                         "auction_price_status", "in_yest_pool", "prior_streak", "p0945_pct",
                         "relative_to_plate_pp", "close_pct", "asof", "relation_asof")
            cols = [key for key in preferred if key in cols]
        else:
            cols = cols[:18]
        auto_hidden_columns.extend(_label(key) or key for key in keys if not any(row.get(key) is not None for row in rows))
        if not cols:
            continue
        header = "".join(f'<th style="text-align:left;padding:8px 7px;background:#e9eef5;color:#475467;font-size:11px">{_esc(_label(k))}</th>' for k in cols)
        body = []
        shown_rows = rows[:5]
        for idx, row in enumerate(shown_rows):
            cells = []
            for key in cols:
                value = row.get(key)
                tone, arrow = _tone(key, value)
                if key == "status" and value is not None:
                    shown = _esc({"FINAL": "终价", "INDICATIVE": "指示价", "READY": "可用", "FRESH": "数据新鲜", "STALE": "数据滞后", "NOT_READY": "尚未就绪"}.get(str(value), value))
                elif key == "in_yest_pool" and isinstance(value, bool):
                    shown = "是" if value else "否"
                else:
                    shown = _num(value, key) if isinstance(value, (int, float)) or value is None else _esc(value)
                if arrow:
                    shown = f'<span style="color:{tone};font-weight:700">{arrow} {shown}</span>'
                cells.append(f'<td style="padding:7px;border-bottom:1px solid #e8edf2;background:{"#ffffff" if idx%2==0 else "#f7f9fb"};font-size:12px;color:{tone}">{shown}</td>')
            body.append("<tr>" + "".join(cells) + "</tr>")
        share_key = next((k for k in ("amount_share_pct", "amount_share", "amount_share_bp", "auction_amount_share", "auction_amount_bp", "share_pct") if k in cols), None)
        bars = []
        if share_key:
            for row in shown_rows[:5]:
                val = row.get(share_key)
                if isinstance(val, (int, float)):
                    divisor = 100 if share_key.endswith("_bp") else 1
                    scale = 100 if "share" in share_key and abs(float(val)) <= 1 else 1
                    bar_width = max(0, min(100, float(val) * scale / divisor))
                    bars.append(f'<div style="display:flex;align-items:center;gap:8px;margin:3px 0;font-size:11px"><span style="width:95px">{_esc(row.get("name", row.get("symbol", "—")))}</span><span style="height:7px;flex:1;background:#edf0f3"><span style="display:block;height:7px;background:#c83b3b;width:{bar_width:.1f}%"></span></span><span>{_num(val,share_key)}</span></div>')
        bar_html = f'<div style="margin-top:8px">{"".join(bars)}</div>' if bars else ""
        plate_appendix = ""
        if chapter_title.startswith("竞价额前十"):
            plate_data = report.get("plates") if isinstance(report.get("plates"), Mapping) else {}
            deferred = plate_data.get("deferred_generic_event")
            if isinstance(deferred, list) and deferred:
                items = "".join(f"<li>{_esc(row.get('name'))}（{_esc(row.get('kind'))}，{_plain_value(row.get('members'), 'members')}只成员）</li>"
                                for row in deferred if isinstance(row, Mapping))
                if items:
                    plate_appendix = f"<details><summary>泛化/事件板块附录（折叠，不参与主榜）</summary><ul>{items}</ul></details>"
        appendix_rows = []
        if len(rows) > 5:
            appendix_limit = 1
            appendix_rows = ["；".join(f"{_label(k)}：{_plain_value(r.get(k), k)}" for k in cols[:3])
                             for r in rows[5:5 + appendix_limit]]
            remaining = len(rows) - 5 - len(appendix_rows)
            extra = f"<p>另有 {remaining} 行未在邮件展开；完整清单见报告数据。</p>" if remaining else ""
            plate_appendix += f'<details><summary>其余 {_num(len(rows)-5)} 行（共 {_num(len(rows))} 行）</summary><ol>' + "".join(
                f"<li>{_esc(line)}</li>" for line in appendix_rows) + f"</ol>{extra}</details>"
        sections.append(f'<h2 style="font-size:16px;color:#23364d;margin:22px 0 8px">{_esc(chapter_title)}（前 {_num(len(shown_rows))} / 共 {_num(len(rows))} 行）</h2><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse"><thead><tr>{header}</tr></thead><tbody>{"".join(body)}</tbody></table>{bar_html}{plate_appendix}')
        plain_sections.append(chapter_title + f"（前{len(shown_rows)}/共{len(rows)}）\n" + "\n".join(" | ".join(f"{_label(k)}：{_plain_value(r.get(k),k)}" for k in cols) for r in shown_rows))
    close_extra = ""
    if isinstance(report.get("email_editorial"), Mapping):
        paragraphs = report["email_editorial"].get("结论", [])
        if paragraphs:
            close_extra = '<h2 style="font-size:16px;margin-top:22px">收盘复盘</h2><ul>' + "".join(f"<li>{_esc(p)}</li>" for p in paragraphs) + "</ul>"
            plain_sections.append("收盘复盘\n" + "\n".join(map(str, paragraphs)))
    hidden_columns = list(dict.fromkeys(hidden_columns + auto_hidden_columns))
    if hidden_columns:
        extra_gap = "整列无有效值，已隐藏：" + "、".join(map(str, hidden_columns))
        gap_text = (gap_text + "；" if gap_text else "") + extra_gap
        gap_html = f'<section style="margin-top:14px;background:#fff4d6;padding:12px 14px"><strong>数据缺口与降级</strong><br>{_esc(gap_text)}</section>'
    gap_html = f'<section style="margin-top:14px;background:#fff4d6;padding:12px 14px"><strong>数据缺口与降级</strong><br>{_esc(gap_text)}</section>' if gap_text else ""
    footer = "口径：缺失显示“—”，不补零。仅事实描述，不构成买卖建议。"
    from .email_board import _front_page_view, _story_view
    story_stage = "close" if any(token in stage_key for token in ("close", "closing", "eod")) else (
        "open" if "open" in stage_key else "auction")
    _story, story_html, story_text = _story_view(report, story_stage, trade_date)
    if story_stage == "close":
        front_html, front_text, appendix_open, appendix_close = "", story_text, "", ""
    else:
        front_html, front_text = _front_page_view(report, story_stage)
        front_html = warning + front_html
        front_text = warning_text + front_text
        appendix_open, appendix_close = '<details><summary style="padding:8px;background:#e9eef5">附表</summary>', "</details>"
    appendix_story = story_text if story_stage == "close" else "故事摘要见首屏"
    plain = quality_plain + "\n\n" + f"{title} | {trade_date}\n" + "\n".join(meta) + "\n\n" + front_text + "\n\n附表\n" + appendix_story + "\n\n一屏结论\n" + "\n".join(f"• {p}" for p in conclusions)
    if metrics:
        plain += "\n\n关键指标\n" + "\n".join(f"{label}：{_num(value,key) if not isinstance(value,str) else value}" for label,value,key in metrics)
    if gap_text:
        plain += "\n\n数据缺口与降级\n" + gap_text
    plain += "\n\n" + "\n\n".join(plain_sections) + "\n\n" + footer
    appendix_story_html = story_html if story_stage == "close" else ""
    content = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark"><title>{_esc(title)}</title><style>@media (prefers-color-scheme: dark) {{ .page{{background:#111827!important;color:#e5e7eb!important}} }}</style></head><body style="margin:0;padding:16px;background:#eef2f6;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#26364a">{quality_html}<table role="presentation" width="100%"><tr><td align="center"><table class="page" role="presentation" width="100%" style="max-width:680px;background:#fff;border-radius:12px"><tr><td style="background:#172b43;padding:22px 24px;color:#fff"><div style="font-size:21px;font-weight:700">{_esc(title)}</div><div style="font-size:12px;padding-top:7px">{_esc(trade_date)}　{meta_html}</div></td></tr><tr><td style="padding:18px 20px">{front_html}{appendix_open}{appendix_story_html}<section style="background:#f6f8fb;padding:14px 16px"><strong>一屏结论</strong><table width="100%">{points_html}</table></section><table width="100%" cellspacing="8" style="margin-top:14px"><tr>{metric_html}</tr></table>{gap_html}{''.join(sections)}{close_extra}{appendix_close}<div style="border-top:1px solid #e5eaf0;margin-top:22px;padding:13px 0;color:#667085;font-size:11px">{_esc(footer)}</div></td></tr></table></td></tr></table></body></html>'''
    if len(content.encode("utf-8")) > MAX_EMAIL_HTML_BYTES:
        raise ValueError(f"rendered email exceeds 100 KB ({len(content.encode('utf-8'))} bytes)")
    if story_stage != "close":
        content = re.sub(r"(?<!\d)\d{6}(?!\d)", "", content)
        plain = re.sub(r"(?<!\d)\d{6}(?!\d)", "", plain)
    return content, plain
