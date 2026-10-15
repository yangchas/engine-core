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
    "p0925_mean": "板块竞价均值", "p0932_mean": "板块09:32均值", "p0945_mean": "板块09:45均值",
    "relative_to_plate_pp": "个股相对板块", "position": "板块内位置", "peer_top3": "板块同伴Top3",
    "amount_64d_median_yuan": "64日成交额中位数", "amount_vs_64d_median": "对64日中位倍数",
    "amount_64d_percentile": "64日成交额分位", "regime_label": "描述性日型",
    "regime_history_sample_days": "历史样本日数", "same_regime_days": "同日型样本数",
    "previous_space_height": "昨日最高板", "max_price_streak": "今日最高板高",
    "top_symbols": "最高板股票", "first_board_count": "今日首板数",
    "previous_limit_n": "昨日涨停家数", "promoted_n": "晋级家数", "broken_n": "断板家数",
    "promotion_rate": "晋级率", "unknown_broken_n": "炸板路径未知数",
    "board_height": "昨日板数", "current_n": "今日收盘涨停价数", "previous_n": "昨日家数",
}

# Explicit number semantics for report fields. Suffix classes below are only
# used for the documented semantic suffixes, never arbitrary key substrings.
FIELD_FORMATS = {
    "ratio": frozenset({"amount_64d_percentile", "close_ge_5_rate", "sealed_up_rate",
                        "turned_negative_rate", "promotion_rate", "auction_amount_share",
                        "auction_amount_share_pct", "auction_amount_share_ratio", "auction_pct_coverage",
                        "anchor_coverage", "source_coverage", "coverage_rate",
                        "retention_rate", "success_rate", "up_share", "down_share", "rate",
                        "share", "regime_history_percentile", "top100_concentration_pct",
                        "p0925_range_position20_pct"}),
    "pp": frozenset({"relative_to_plate_pp", "delta_pp", "change_pp", "close_vs_p0945_pp",
                     "move_0924_0925_pp"}),
    "multiple": frozenset({"amount_vs_prev_common", "amount_vs_64d_median_common",
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


def _metrics(report: Mapping[str, Any], stage: str) -> list[tuple[str, Any, str]]:
    if stage == "close" or "market" in report:
        market = report.get("market") if isinstance(report.get("market"), dict) else {}
        candidates = [("全天成交额", market.get("amount_yuan"), "amount_yuan"),
                      (f"对前日倍数（固定共同池 {_plain_value(market.get('amount_common_pool_n'))} 只）", market.get("amount_vs_prev_common"), "vs_amount"),
                      (f"对64日中位数（固定共同池 {_plain_value(market.get('amount_64d_common_pool_n'))} 只）", market.get("amount_vs_64d_median_common"), "vs_amount"),
                      ("64日成交额分位", market.get("amount_64d_percentile"), "rate"),
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
            candidates += [("数据时点", volume.get("asof"), ""), ("数据滞后秒", volume.get("data_lag_seconds"), ""),
                           ("有价标的数", volume.get("priced_symbol_count"), "count")]
    return [(label, value, key) for label, value, key in candidates if value is not None]


def _chapters(report: Mapping[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    chapters = report.get("chapters") if isinstance(report.get("chapters"), dict) else {}
    out = []
    for key, value in chapters.items():
        label = _label(key)
        if label and isinstance(value, list) and value and isinstance(value[0], dict):
            out.append((label, value[:12]))
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
        if summary:
            out.append(("大盘竞价/开盘量价", [summary]))
        top_plates = market_volume.get("top_plates_by_share")
        if isinstance(top_plates, list) and top_plates:
            out.append(("板块竞价额份额与涨幅（成员重叠，不可相加）", top_plates[:10]))
        top_stocks = market_volume.get("top_stocks_by_amount")
        if isinstance(top_stocks, list) and top_stocks:
            out.append(("个股竞价额前列", top_stocks[:10]))
    look = chapters.get("look_a_do_b")
    if isinstance(look, list) and look:
        out.append(("看A做B", look[:10]))
    if isinstance(chapters.get("look_a_do_b"), list) and not chapters["look_a_do_b"]:
        out.append(("看A做B", [{"status": "当前报告无匹配样本"}]))
    if "market" in report:
        for key in ("big_meat", "big_loss"):
            val = report.get(key)
            if isinstance(val, list) and val and isinstance(val[0], dict):
                out.append(("大肉" if key == "big_meat" else "大面", val[:4]))
        for key, title in (("plate_evolution", "个股—板块演化"), ("ladder_summary", "连板梯队"),
                           ("unlimited_price_band_stocks", "新股/无涨跌幅限制")):
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
            out.append(("新股/无涨跌幅限制（价格带例外）", exceptional_rows[:10]))
        plates = report.get("plates")
        if isinstance(plates, dict):
            for key in ("close_top10", "close_bottom10", "auction_amount_top10"):
                val = plates.get(key)
                if isinstance(val, list) and val:
                    title = FIELD_LABELS[key]
                    if key == "auction_amount_top10":
                        title += "（成员重叠，不可相加）"
                    out.append((title, val[:10]))
        missed = report.get("missed_opportunities")
        if isinstance(missed, Mapping) and isinstance(missed.get("plate_counts"), Mapping):
            rows = [{"name": name, "count": count} for name, count in sorted(
                missed["plate_counts"].items(), key=lambda item: (-item[1], item[0]))[:10]]
            if rows:
                out.append(("漏掉机会的板块分布", rows))
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
    return out[:24]


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
    generated_gaps = []
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
        if chapter_title in {"大肉", "大面"}:
            preferred = ("symbol", "name", "main_plate", "limit_pct", "auction_pct", "auction_price_status", "p0932_pct",
                         "p0945_pct", "close_pct", "high_pct", "low_pct", "delta_over_limit",
                         "auction_amount_yuan", "auction_amount_bp", "in_yest_pool", "lb_days",
                         "visibility_half_limit")
            cols = [key for key in preferred if key in cols]
        else:
            cols = cols[:18]
        auto_hidden_columns.extend(_label(key) or key for key in keys if not any(row.get(key) is not None for row in rows))
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
            for row in rows[:5]:
                val = row.get(share_key)
                if isinstance(val, (int, float)):
                    divisor = 100 if share_key.endswith("_bp") else 1
                    scale = 100 if "share" in share_key and abs(float(val)) <= 1 else 1
                    bar_width = max(0, min(100, float(val) * scale / divisor))
                    bars.append(f'<tr><td style="padding:3px 6px;font-size:11px">{_esc(row.get("name", row.get("symbol", "—")))}</td><td style="width:72%;padding:3px"><table role="presentation" width="100%"><tr><td style="height:7px;background:#c83b3b;width:{bar_width:.1f}%;font-size:0">&nbsp;</td><td style="height:7px;background:#edf0f3;font-size:0">&nbsp;</td></tr></table></td><td style="font-size:11px;padding:3px">{_num(val,share_key)}</td></tr>')
        bar_html = f'<table role="presentation" width="100%" style="margin-top:8px">{"".join(bars)}</table>' if bars else ""
        plate_appendix = ""
        if chapter_title.startswith("竞价额前十"):
            plate_data = report.get("plates") if isinstance(report.get("plates"), Mapping) else {}
            deferred = plate_data.get("deferred_generic_event")
            if isinstance(deferred, list) and deferred:
                items = "".join(f"<li>{_esc(row.get('name'))}（{_esc(row.get('kind'))}，{_plain_value(row.get('members'), 'members')}只成员）</li>"
                                for row in deferred if isinstance(row, Mapping))
                if items:
                    plate_appendix = f"<details><summary>泛化/事件板块附录（折叠，不参与主榜）</summary><ul>{items}</ul></details>"
        sections.append(f'<h2 style="font-size:16px;color:#23364d;margin:22px 0 8px">{_esc(chapter_title)}</h2><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse"><thead><tr>{header}</tr></thead><tbody>{"".join(body)}</tbody></table>{bar_html}{plate_appendix}')
        plain_sections.append(chapter_title + "\n" + "\n".join(" | ".join(f"{_label(k)}：{_plain_value(r.get(k),k)}" for k in cols) for r in rows))
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
