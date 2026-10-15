"""Descriptive post-close review of auction, intraday and close facts."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence

from .daily_kline_capture import load_capture
from .extremes_review import (REGIME_LABELS, _num, assemble_from_files, build_daily_history,
                              compare_history, derive_extremes_review, limit_percent)
from .plate_kind import classify_plate_kind
from .a_to_b import _prior_streak, build_a_to_b, select_main_plate
from .ladder_section import build_ladder_section
from .extremes_board import build_extremes_board
from .limit_price import is_limit_up, is_limit_down, state_at_close


def attach_close_sections(data: dict[str, Any], *, relation_asof: str = "UNKNOWN",
                         plate_metadata: Mapping[str, Mapping[str, Any]] | None = None,
                         relations: Mapping[str, Sequence[str]] | None = None,
                         ladder_rows: Sequence[Mapping[str, Any]] | None = None,
                         ladder_summary: Sequence[Mapping[str, Any]] = (),
                         path_frequencies: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Attach the same A→B and ladder chapters to every close report path."""
    trade_date = str(data.get("trade_date") or "")
    stocks = data.get("stocks") if isinstance(data.get("stocks"), list) else []
    asof = f"{trade_date}T15:00:00" if trade_date else "UNKNOWN"
    path_by_symbol = {str(row.get("symbol", row.get("code", ""))).zfill(6): row.get("path_type")
                      for row in ladder_rows or () if row.get("path_type")}
    for row in stocks:
        symbol = str(row.get("symbol", row.get("code", ""))).zfill(6)
        if symbol in path_by_symbol:
            row["path_type"] = path_by_symbol[symbol]
    market_state = dict(data.get("market") or {})
    if isinstance(data.get("regime_history"), Mapping):
        market_state["regime_history"] = data["regime_history"]
    a_to_b = build_a_to_b(stocks, trade_date=trade_date, stage="close", asof=asof,
                          relation_asof=relation_asof, relations=relations,
                          plate_metadata=plate_metadata, market_state=market_state,
                          path_frequencies=path_frequencies,
                          relation_lag_trading_days=(data.get("data_quality") or {}).get("relation_lag_trading_days"))
    data["a_to_b"] = a_to_b
    data["a_to_b_schema_version"] = 2
    data["a_to_b_status"] = "READY" if a_to_b else "NO_QUALIFIED_A"
    unlimited = [str(row.get("symbol", row.get("code", ""))).zfill(6) for row in stocks
                 if row.get("is_unlimited_price_band") or
                 row.get("price_band_label") in {"新股/无涨跌幅限制", "新股/无涨跌停价"} or
                 row.get("auction_price_status") in {"UNLIMITED", "无涨跌停价", "无涨跌幅限制"}]
    data["a_to_b_excluded_unlimited"] = unlimited
    if unlimited:
        gaps = data.setdefault("data_gaps", [])
        if not isinstance(gaps, list):
            gaps = data["data_gaps"] = [str(gaps)]
        gaps.append(f"新股/无涨跌停价单列，不进入A→B：{len(unlimited)}只")
    by_symbol = {str(row.get("symbol", row.get("code", ""))).zfill(6): row for row in stocks}
    rows: list[dict[str, Any]] = []
    for source in ladder_rows or ():
        row = dict(source)
        row["symbol"] = str(row.get("symbol", row.get("code", ""))).zfill(6)
        rows.append(row)
    existing = {row["symbol"] for row in rows}
    for source in stocks:
        symbol = str(source.get("symbol", source.get("code", ""))).zfill(6)
        streak, _conflict = _prior_streak(source)
        if symbol in existing or not (streak is not None and streak > 0 or
                                      source.get("in_yest_pool") in (True, 1, "1")):
            continue
        row = dict(source)
        rows.append(row)
        existing.add(symbol)
    for group in a_to_b:
        symbol = group["a_symbol"]
        if symbol not in existing and symbol in by_symbol:
            row = dict(by_symbol[symbol])
            rows.append(row)
            existing.add(symbol)
    data["ladder_section"] = build_ladder_section(
        rows, stage="close", asof=asof, frequencies=path_frequencies,
        summary=ladder_summary, market_stocks=stocks)
    if not a_to_b:
        gaps = data.setdefault("data_gaps", [])
        if not isinstance(gaps, list):
            gaps = data["data_gaps"] = [str(gaps)]
        gaps.append("A→B 无候选：无满足昨日≥2板或竞价涨停价且有封单的 A")
    return data


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _mean(values: Sequence[float]) -> float | None:
    return statistics.mean(values) if values else None


def _rate(n: int, d: int) -> float | None:
    return n / d if d else None


def _fixed_common_amounts(today: Mapping[str, float], previous: Mapping[str, float],
                          history: Sequence[Mapping[str, float]]) -> dict[str, Any]:
    common = set(today) & set(previous)
    for day in history:
        common.intersection_update(day)
    history_totals = [sum(day[symbol] for symbol in common) for day in history]
    today_total = sum(today[symbol] for symbol in common)
    previous_total = sum(previous[symbol] for symbol in common)
    history_median = _median(history_totals)
    history_percentile = (sum(value <= today_total for value in history_totals) / len(history_totals)
                          if history_totals else None)
    return {"symbols": common, "today": today_total, "previous": previous_total,
            "history_median": history_median,
            "history_percentile": history_percentile,
            "today_vs_previous": today_total / previous_total if previous_total else None,
            "today_vs_history_median": today_total / history_median if history_median else None}


def _sym(value: Any) -> str:
    value = str(value or "").strip().upper()
    return value.zfill(6) if value.isdigit() and len(value) < 6 else value


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _listed_symbols(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        symbol = value.get("symbol", value.get("code", value.get("stockid")))
        if symbol is not None:
            found.add(_sym(symbol))
        for key, child in value.items():
            if key in ("symbol", "code", "stockid"):
                continue
            found.update(_listed_symbols(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_listed_symbols(child))
    return found


def _live_prompted(live: Mapping[str, Any] | None) -> set[str]:
    if not live:
        return set()
    chapters = live.get("chapters", {})
    # These are the explicitly accepted page cues. Do not treat generic watchlists
    # or all stocks in the page as prompts.
    keys = ("emerging", "plate_changes", "look_a_do_b", "yesterday_limit_pool",
            "yesterday_highboards")
    return set().union(*(_listed_symbols(chapters.get(key)) for key in keys)) if isinstance(chapters, Mapping) else set()


def _profile(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    delta = [r["delta_over_limit"] for r in rows if r.get("delta_over_limit") is not None]
    closes = [r["close_pct"] for r in rows if r.get("close_pct") is not None]
    return {"n": len(delta), "close_mean": _mean(closes), "close_median": _median(closes),
            "big_meat_count": sum(v >= .5 for v in delta), "big_loss_count": sum(v <= -.5 for v in delta)}


def _prev_categories(prev: Mapping[str, Any] | None) -> dict[str, list[str]]:
    if not prev:
        return {}
    chapters = prev.get("chapters", {})
    if not isinstance(chapters, Mapping):
        return {}
    out: dict[str, list[str]] = {}
    for key, label in (("yesterday_highboards", "昨日高标"), ("look_a_do_b", "看A做B"),
                       ("yesterday_limit_pool", "昨日涨停池")):
        ids = sorted(_listed_symbols(chapters.get(key)))
        if ids:
            out[label] = ids
    watch = chapters.get("watchlist", {})
    if isinstance(watch, Mapping):
        for key, label in (("yesterday_limit_pool", "昨日涨停池(观察)"),):
            ids = sorted(_listed_symbols(watch.get(key)))
            if ids and label not in out:
                out[label] = ids
    return out


def _quadrant(row: Mapping[str, Any]) -> str | None:
    strength, net = _num(row.get("strength")), _num(row.get("net_yuan"))
    if strength is None or net is None:
        return None
    if strength >= 0 and net >= 0: return "双正"
    if strength < 0 and net < 0: return "双负"
    if strength >= 0: return "强度正净流出背离"
    return "强度负净流入背离"


def derive_close_review(rows: Sequence[Mapping[str, Any]], *, trade_date: str,
                        live_report: Mapping[str, Any] | None = None,
                        prev_live: Mapping[str, Any] | None = None,
                        market: Mapping[str, Any] | None = None,
                        plates: Sequence[Mapping[str, Any]] | None = None,
                        kaipan_snapshots: Sequence[Mapping[str, Any]] | None = None,
                        regime_history: Mapping[str, Any] | None = None,
                        checkpoint_status: Mapping[str, str] | None = None,
                        extremes_k_abs: int = 20, extremes_k_amount: int = 20,
                        extremes_min_auction_amount_yuan: float = 30_000_000,
                        extremes_threshold_pct: float = 3.0) -> dict[str, Any]:
    """Derive compact close-review sections from normalized per-stock observations."""
    clean = []
    for source in rows:
        r = dict(source)
        r["symbol"] = _sym(r.get("symbol", r.get("code")))
        r.setdefault("limit_pct", limit_percent(r["symbol"], str(r.get("name") or "")))
        if r["symbol"].startswith("302"):
            r["limit_pct"] = 20.0
        r["auction_pct"] = _num(r.get("auction_pct")); r["close_pct"] = _num(r.get("close_pct"))
        if r.get("delta_pp") is None and r["auction_pct"] is not None and r["close_pct"] is not None:
            r["delta_pp"] = r["close_pct"] - r["auction_pct"]
        r["delta_pp"] = _num(r.get("delta_pp"))
        band = _num(r.get("limit_pct"))
        high_pct, low_pct = _num(r.get("high_pct")), _num(r.get("low_pct"))
        r["is_unlimited_price_band"] = bool(band and any(
            value is not None and abs(value) > band * 1.5
            for value in (r["close_pct"], high_pct, low_pct)))
        r["price_band_label"] = "新股/无涨跌幅限制" if r["is_unlimited_price_band"] else None
        r["delta_over_limit"] = r["delta_pp"] / band if r["delta_pp"] is not None and band else None
        r["is_big_meat"] = not r["is_unlimited_price_band"] and r["delta_over_limit"] is not None and r["delta_over_limit"] >= .5
        r["is_big_loss"] = not r["is_unlimited_price_band"] and r["delta_over_limit"] is not None and r["delta_over_limit"] <= -.5
        unlimited = bool(r.get("is_unlimited_price_band") or r.get("unlimited_price_band"))
        r["close_price_state"] = state_at_close(r.get("close"), r.get("high"), r.get("low"),
                                                r.get("pre_close"), r["symbol"],
                                                is_st=bool(r.get("is_st")), unlimited=unlimited)
        r["close_at_limit_up"] = is_limit_up(r.get("close"), r.get("pre_close"), r["symbol"],
                                             is_st=bool(r.get("is_st")), unlimited=unlimited)
        r["close_at_limit_down"] = is_limit_down(r.get("close"), r.get("pre_close"), r["symbol"],
                                                 is_st=bool(r.get("is_st")), unlimited=unlimited)
        r["visibility"] = "已观测检查点待计算"
        clean.append(r)
    checkpoint_status = dict(checkpoint_status or {})
    missing_checkpoints = [tag for tag in ("0932", "0935", "0940", "0945")
                           if checkpoint_status.get(tag, "READY") != "READY"]
    if any(tag in missing_checkpoints for tag in ("0932", "0945")):
        for row in clean:
            row["visibility"] = f"不可判断：{','.join(tag for tag in ('0932','0945') if tag in missing_checkpoints)}检查点缺失"
    base = derive_extremes_review(clean, trade_date=trade_date, plates=plates or ())
    market_auction_amount = sum(r.get("auction_amount_yuan") or 0 for r in clean
                                if r.get("auction_amount_yuan") is not None)
    for r in clean:
        amount = r.get("auction_amount_yuan")
        r["auction_amount_share"] = amount / market_auction_amount if amount is not None and market_auction_amount else None
        r["auction_amount_bp"] = r["auction_amount_share"] * 10_000 if r["auction_amount_share"] is not None else None
        band = _num(r.get("limit_pct"))
        checkpoints = [(tag, _num(r.get(key))) for tag, key in (("0932", "p0932_pct"), ("0935", "p0935_pct"),
                      ("0940", "p0940_pct"), ("0945", "p0945_pct"))]
        auction_pct = _num(r.get("auction_pct"))
        if (not band or auction_pct is None or not any(value is not None for _, value in checkpoints)
                or any(tag in missing_checkpoints for tag in ("0932", "0945"))):
            r["visibility_half_limit"] = "不可判断：板幅或检查点缺失"
        else:
            threshold = .5 if r.get("is_big_meat") else -.5
            reached = [tag for tag, value in checkpoints if value is not None and
                       ((value - auction_pct) / band >= threshold if threshold > 0 else (value - auction_pct) / band <= threshold)]
            r["visibility_half_limit"] = (f"{reached[0]}首次已观测达到0.5L" if reached else
                "在已观测检查点未达到0.5L；点间时刻UNKNOWN")
            r["visibility"] = "在已观测检查点达到0.5L" if reached else "在已观测检查点未达到0.5L"
    meat = sorted((r for r in clean if r.get("is_big_meat")), key=lambda r: (-(r.get("auction_amount_yuan") or 0), r["symbol"]))[:20]
    loss = sorted((r for r in clean if r.get("is_big_loss")), key=lambda r: (-(r.get("auction_amount_yuan") or 0), r["symbol"]))[:20]
    prompt = _live_prompted(live_report)
    missed = [r for r in clean if r.get("is_big_meat") and (r.get("auction_pct") is not None and r["auction_pct"] < 3)
              and (r["symbol"] not in prompt)] if live_report else []
    plate_counts: dict[str, int] = {}
    for r in missed:
        names = r.get("plates") or ([r.get("main_plate")] if r.get("main_plate") else [])
        for name in names[:2]:
            if name: plate_counts[str(name)] = plate_counts.get(str(name), 0) + 1
    if live_report:
        missed_section = {"status": "READY", "symbols": [r["symbol"] for r in missed], "count": len(missed),
                          "plate_counts": dict(sorted(plate_counts.items(), key=lambda x: (-x[1], x[0])))}
    else:
        missed_section = {"status": "UNAVAILABLE_NO_LIVE_REPORT", "symbols": None, "count": None, "plate_counts": None}

    by_symbol = {r["symbol"]: r for r in clean}
    eligible = [r for r in clean if r.get("p0945_pct") is not None and r.get("p0945_cumulative_amount_yuan") is not None
                and not r.get("is_unlimited_price_band")]
    live_emerging_ids = _listed_symbols((live_report or {}).get("chapters", {}).get("emerging")) if live_report else set()
    # The page is only a candidate set. The reported strict cohort is rebuilt
    # from the 09:45 checkpoint and its exact >=5% threshold.
    emerging_rows = [r for r in eligible if r["symbol"] in live_emerging_ids and r["p0945_pct"] >= 5]
    emerging_ids = [r["symbol"] for r in emerging_rows]
    k = len(emerging_rows)
    def score(group: Sequence[Mapping[str, Any]], expected: int | None = None) -> dict[str, Any]:
        n = len(group)
        return {"n": n if expected is None else expected, "matched": n,
                "symbols": [r["symbol"] for r in group],
                "close_ge_5": sum(r.get("close_pct") is not None and r["close_pct"] >= 5 for r in group),
                "sealed_up": sum(r.get("close_at_limit_up", False) for r in group),
                "turned_negative": sum(r.get("close_pct") is not None and r["close_pct"] < 0 for r in group),
                "close_ge_5_rate": _rate(sum(r.get("close_pct") is not None and r["close_pct"] >= 5 for r in group), n),
                "sealed_up_rate": _rate(sum(r.get("close_at_limit_up", False) for r in group), n),
                "turned_negative_rate": _rate(sum(r.get("close_pct") is not None and r["close_pct"] < 0 for r in group), n)}
    p945_ranked = sorted(eligible, key=lambda r: (-r["p0945_pct"], r["symbol"]))[:k]
    amount_ranked = sorted(eligible, key=lambda r: (-r["p0945_cumulative_amount_yuan"], r["symbol"]))[:k]
    scorecard = {"status": ("UNAVAILABLE_NO_LIVE_REPORT" if not live_report else
                             "DEGRADED" if "0945" in missing_checkpoints else "READY"),
                 "emerging": score(emerging_rows, k) if live_report else None,
                 "p0945_top_k": score(p945_ranked) if live_report and "0945" not in missing_checkpoints else None,
                 "amount_top_k": score(amount_ranked) if live_report else None, "k": k if live_report else None}

    yesterday = _prev_categories(prev_live)
    if not prev_live or not yesterday:
        yesterday_section = {"status": "UNAVAILABLE_NO_PREV_REPORT", "categories": {}}
    else:
        cats = {}
        for label, ids in yesterday.items():
            group = [by_symbol[s] for s in ids if s in by_symbol]
            cats[label] = {**_profile(group), "expected_n": len(ids), "matched_n": len(group),
                           "symbols": ids}
        yesterday_section = {"status": "READY", "categories": cats}

    # Close plate returns and auction-amount share use only known members/values.
    plate_rows = []
    deferred_plate_rows = []
    for plate in plates or ():
        name = plate.get("name") or plate.get("code")
        members = [r for r in clean if name in (r.get("plates") or [])]
        closes = [r["close_pct"] for r in members if r.get("close_pct") is not None]
        auction = [r["p0925_pct"] for r in members if r.get("p0925_pct") is not None]
        p932 = [r["p0932_pct"] for r in members if r.get("p0932_pct") is not None]
        p945 = [r["p0945_pct"] for r in members if r.get("p0945_pct") is not None]
        amounts = [r["auction_amount_yuan"] for r in members if r.get("auction_amount_yuan") is not None]
        kind = plate.get("kind") or classify_plate_kind(str(name or ""))
        if len(members) >= 8 and closes:
            row = {"name": name, "kind": kind, "members": len(members), "close_mean": _mean(closes),
                   "p0925_mean": _mean(auction), "p0932_mean": _mean(p932), "p0945_mean": _mean(p945),
                   "auction_amount_yuan": sum(amounts) if amounts else None}
            if plate.get("generic") or kind in {"GENERIC", "EVENT", "NOISE"}:
                deferred_plate_rows.append(row)
            else:
                plate_rows.append(row)
    total_plate_amt = market_auction_amount
    for p in plate_rows:
        p["auction_amount_share"] = _rate(p["auction_amount_yuan"], total_plate_amt) if p["auction_amount_yuan"] is not None else None
        p["auction_amount_bp"] = p["auction_amount_share"] * 10_000 if p["auction_amount_share"] is not None else None
    top_amt = sorted((p for p in plate_rows if p["auction_amount_share"] is not None), key=lambda p: (-p["auction_amount_share"], p["name"]))[:10]
    narrow_top = sorted((p for p in plate_rows if p["auction_amount_share"] is not None
                         and 8 <= p["members"] <= 150 and p["kind"] in {"THEME", "NICHE"}),
                        key=lambda p: (-p["auction_amount_share"], p["name"]))[:5]
    for p in deferred_plate_rows:
        p["auction_amount_share"] = _rate(p["auction_amount_yuan"], total_plate_amt) if p["auction_amount_yuan"] is not None else None
    deferred_top = sorted(deferred_plate_rows, key=lambda p: (-(p.get("auction_amount_yuan") or 0), p["name"]))[:20]
    plate_rows.sort(key=lambda p: (p["close_mean"], p["name"]))
    plate_section = {"status": "READY" if plates else "UNAVAILABLE_NO_RELATION",
                     "close_top10": list(reversed(plate_rows[-10:])), "close_bottom10": plate_rows[:10],
                     "auction_amount_top10": top_amt,
                     "narrow_theme_top5": narrow_top,
                     "deferred_generic_event": deferred_top,
                     "top_bottom_overlap": len({p["name"] for p in plate_rows[-10:]} & {p["name"] for p in top_amt}) if plates else None,
                     "quadrant_retention": None}
    snapshots = list(kaipan_snapshots or [])
    if snapshots:
        parsed: dict[str, dict[str, str]] = {}
        for snap in snapshots:
            tag = str(snap.get("tag") or snap.get("captured_local", ""))
            current = {}
            for row in snap.get("rows", []):
                q = _quadrant(row)
                if q and row.get("code"): current[str(row["code"])] = q
            parsed[tag] = current
        times = sorted(parsed)
        end = next((t for t in reversed(times) if "15:05" in t or "150500" in t), times[-1])
        starts = [t for t in times if "10:30" in t or "1030" in t or "10:54" in t or "1054" in t]
        starts = [t for t in starts if t != end] or [times[0]]
        retentions = []
        for start in starts:
            common = set(parsed[start]) & set(parsed[end])
            retained = sum(parsed[start][p] == parsed[end][p] for p in common)
            retentions.append({"from": start, "to": end, "matched": len(common),
                               "retained": retained, "rate": _rate(retained, len(common))})
        plate_section["quadrant_retention"] = retentions

    market_section = dict(market or {})
    returns = [r["close_pct"] for r in clean if r.get("close_pct") is not None]
    market_section.setdefault("close_median", _median(returns))
    market_section.setdefault("up_count", sum(x > 0 for x in returns))
    market_section.setdefault("down_count", sum(x < 0 for x in returns))
    market_section.setdefault("flat_count", sum(x == 0 for x in returns))
    market_section.setdefault("close_limit_up_count", base["summary"]["close_limit_up_count"])
    market_section.setdefault("close_limit_down_count", base["summary"]["close_limit_down_count"])
    market_section.setdefault("regime", base.get("regime"))
    market_section.setdefault("regime_label", REGIME_LABELS.get(base.get("regime"), base.get("regime")))
    market_section.setdefault("n", len(returns))
    p945_values = [r["p0945_pct"] for r in clean if r.get("p0945_pct") is not None]
    market_section.setdefault("p0945_median", _median(p945_values))
    market_section.setdefault("close_vs_p0945_pp", (market_section.get("close_median") - _median(p945_values))
                              if p945_values and market_section.get("close_median") is not None else None)
    auction_values = [r["auction_pct"] for r in clean if r.get("auction_pct") is not None]
    auction_amounts = [r["auction_amount_yuan"] for r in clean if r.get("auction_amount_yuan") is not None]
    from .extremes_review import classify_regime
    market_section.setdefault("auction_amount_yuan", sum(auction_amounts) if auction_amounts else None)
    market_section.setdefault("auction_median", _median(auction_values))
    market_section.setdefault("auction_up_count", sum(x > 0 for x in auction_values))
    market_section.setdefault("auction_down_count", sum(x < 0 for x in auction_values))
    market_section.setdefault("auction_regime", classify_regime(
        left_tail=sum(x <= -8 for x in auction_values), right_tail=sum(x >= 8 for x in auction_values),
        market_median=_median(auction_values), up_rate=_rate(sum(x > 0 for x in auction_values), len(auction_values))) if auction_values else None)
    if "auction_comparison" not in market_section:
        if market_section["auction_amount_yuan"] is not None and market_section["auction_median"] is not None:
            market_section["auction_comparison"] = (
                f"0925竞价额 {market_section['auction_amount_yuan']/1e8:.2f}亿元；竞价中位 {market_section['auction_median']:+.2f}%、"
                f"日型 {REGIME_LABELS.get(market_section['auction_regime'], market_section['auction_regime'])}；"
                f"收盘中位 {market_section['close_median']:+.2f}%、日型 {market_section['regime_label']}")
        else:
            market_section["auction_comparison"] = "竞价金额/日型对照缺少可用输入"

    plate_meta = {str(p.get("name")): {"class": p.get("kind", p.get("class")),
                                        "size": p.get("members", p.get("size"))}
                  for p in plates or () if p.get("name")}
    candidate_names = {str(name) for row in clean for name in
                       (row.get("plates") or ([row.get("main_plate")] if row.get("main_plate") else []))}
    for row in clean:
        names = [str(name) for name in (row.get("plates") or [])]
        sources = {str(row.get(f"{source}_plate")): source.upper()
                   for source in ("pool", "reason", "static", "hot") if row.get(f"{source}_plate")}
        chosen = select_main_plate(names, plate_meta, source_candidates=sources)
        row["main_plate"] = chosen
        row["main_plate_reason"] = None if chosen else "无合格主题板块"
    by_plate: dict[str, list[Mapping[str, Any]]] = {}
    for row in clean:
        name = row.get("main_plate")
        if name:
            by_plate.setdefault(str(name), []).append(row)
    focus_symbols = {r["symbol"] for r in meat[:8] + loss[:8]}
    plate_evolution = []
    excluded_plate_names = {str(p.get("name")) for p in plates or ()
                            if p.get("generic") or
                            str(p.get("kind") or p.get("class") or classify_plate_kind(str(p.get("name") or ""))).upper()
                            not in {"THEME", "NICHE"}}
    for name, members in by_plate.items():
        if name in excluded_plate_names:
            continue
        focus = [r for r in members if r["symbol"] in focus_symbols]
        if not focus:
            continue
        for row in focus:
            values = [r.get("p0925_pct") for r in members], [r.get("p0932_pct") for r in members], [r.get("p0945_pct") for r in members], [r.get("close_pct") for r in members]
            trajectory = [_mean([x for x in xs if x is not None]) for xs in values]
            peers = []
            for x in members:
                if x["symbol"] == row["symbol"]:
                    continue
                pct = _num(x.get("p0925_pct", x.get("auction_pct")))
                amount = _num(x.get("auction_amount_yuan"))
                limit = _num(x.get("limit_pct"))
                state = x.get("auction_limit_state")
                if (x.get("in_yest_pool") in (True, 1, "1") or x.get("halted") is True
                    or x.get("is_halted") is True or x.get("trading_status") in {"HALTED", "停牌"}
                    or state in (1, -1, "1", "-1", "UP", "DOWN", "LIMIT_UP", "LIMIT_DOWN")
                    or pct is None or amount is None
                    or (limit is not None and abs(pct) >= limit - 0.2)):
                    continue
                common = sum(1 for candidate in (x.get("plates") or [])
                             if candidate in candidate_names and candidate in (row.get("plates") or [])
                             and candidate not in excluded_plate_names
                             and (not plate_meta or select_main_plate([candidate], plate_meta) is not None))
                peers.append((amount, common, x))
            peer_rows = [entry[2] for entry in sorted(peers,
                         key=lambda item: (-item[0], -item[1], item[2]["symbol"]))[:5]]
            peer_common = {entry[2]["symbol"]: entry[1] for entry in peers}
            first_plate_for_symbol = any(item.get("plate") == name for item in plate_evolution)
            rel = (row.get("close_pct") - trajectory[-1]) if row.get("close_pct") is not None and trajectory[-1] is not None else None
            plate_evolution.append({"symbol": row["symbol"], "name": row.get("name"), "plate": name,
                "p0925_pct": trajectory[0], "p0932_pct": trajectory[1], "p0945_pct": trajectory[2],
                "close_pct": trajectory[3], "stock_close_pct": row.get("close_pct"),
                "relative_to_plate_pp": rel,
                "position": "领涨" if rel is not None and rel >= 2 else "落后板块" if rel is not None and rel <= -2 else "跟随板块",
                "peer_top3": (f"同板块 {name}；" if first_plate_for_symbol else "") + "按竞价额选，非事后收益；" + "；".join(
                    f"{x['symbol']} {x.get('name') or ''} 共同合格板块数 {peer_common.get(x['symbol'], 0)}；20日区间位置 "
                    f"{('高于前20日区间' if x.get('p0925_range_position20_pct') > 100 else '低于前20日区间') if x.get('p0925_range_position20_pct') is not None and (x.get('p0925_range_position20_pct') > 100 or x.get('p0925_range_position20_pct') < 0) else ((format(x.get('p0925_range_position20_pct'), '.0f') + '%') if x.get('p0925_range_position20_pct') is not None else 'UNKNOWN')} "
                    f"0945 {(format(x.get('p0945_pct'), '+.2f') + '%') if x.get('p0945_pct') is not None else '—'} 竞价额 {((x.get('auction_amount_yuan') or 0)/1e4):.0f}万"
                    for x in peer_rows)})
    unlimited_rows = [r for r in clean if r.get("is_unlimited_price_band")]
    market_section["unlimited_price_band_count"] = len(unlimited_rows)
    extremes_board = build_extremes_board(
        clean, stage="close", k_abs=extremes_k_abs, k_amount=extremes_k_amount,
        min_auction_amount_yuan=extremes_min_auction_amount_yuan,
        threshold_pct=extremes_threshold_pct)
    return {"contract": "CloseReviewV1", "trade_date": trade_date, "market": market_section,
            "extremes_board": extremes_board,
            "big_meat": meat, "big_loss": loss, "missed_opportunities": missed_section,
            "yesterday_review": yesterday_section, "emerging_scorecard": scorecard,
            "plates": plate_section, "regime_history": regime_history,
            "checkpoint_chapters": {"timeline": {"status": "DEGRADED" if missing_checkpoints else "READY",
                                                       "missing": missing_checkpoints,
                                                       "reason": "缺少必需盘中检查点" if missing_checkpoints else None},
                                     "visibility": {"status": "DEGRADED" if any(tag in missing_checkpoints for tag in ("0932", "0945")) else "READY",
                                                    "missing": [tag for tag in missing_checkpoints if tag in ("0932", "0945")],
                                                    "reason": "无法判断是否在0945前可见" if any(tag in missing_checkpoints for tag in ("0932", "0945")) else None}},
            "stocks": clean, "plate_evolution": plate_evolution[:20],
            "main_plate_gaps": [{"symbol": r["symbol"], "name": r.get("name"),
                                  "reason": "无合格主题板块"}
                                 for r in clean if not r.get("main_plate")],
            "unlimited_price_band_stocks": sorted(unlimited_rows, key=lambda r: -(r.get("auction_amount_yuan") or 0))[:10],
            "data_quality": {"included_stock_count": len(clean),
                             "auction_pct_coverage": sum(r.get("auction_pct") is not None for r in clean),
                             "close_pct_coverage": len(returns),
                             "p0932_coverage": sum(r.get("p0932_pct") is not None for r in clean),
                             "p0945_coverage": sum(r.get("p0945_pct") is not None for r in clean),
                             "indicative_count": sum(r.get("auction_price_status") == "INDICATIVE" for r in clean),
                             "final_count": sum(r.get("auction_price_status") == "FINAL" for r in clean),
                             "daily_source": market_section.get("daily_source", "UNKNOWN")}}


def render_close_markdown(data: Mapping[str, Any]) -> str:
    f = lambda v: "—" if v is None else f"{v:+.2f}"
    pct = lambda v: "—" if v is None else f"{v*100:.1f}%"
    m = data["market"]
    lines = [f"# 收盘复盘 {data['trade_date']}", "", "## 大盘收盘", "",
             f"收盘中位 {f(m.get('close_median'))}%；涨/跌/平 {m.get('up_count','—')}/{m.get('down_count','—')}/{m.get('flat_count','—')}；收盘涨停价代理/跌停价代理 {m.get('close_limit_up_count','—')}/{m.get('close_limit_down_count','—')}；全天成交额 {f(m.get('amount_yuan'))} 元；固定共同池({m.get('amount_common_pool_n','—')}只)前日倍数 {f(m.get('amount_vs_prev_common'))}×；64日共同池({m.get('amount_64d_common_pool_n','—')}只)中位数倍数 {f(m.get('amount_vs_64d_median_common'))}×。",
             f"日型 {m.get('regime_label','—')}；{m.get('auction_comparison','竞价日型/金额对照缺失')}。", "",
             "## 竞价先选、后追踪极值（当日描述）", "",
             "选入集合仅使用≤09:25竞价涨幅绝对值前K与竞价金额前K的并集；金额阈值默认3000万元，FINAL/INDICATIVE按来源标注。", "",
             "名称 | 板块 | 入选 | 竞价% | 额(亿) | 0932 | 0945 | 收盘% | Δ(pp) | 状态", "---|---|---|---:|---:|---:|---:|---:|---:|---"]
    board_rows = data.get("extremes_board", {}).get("close", {}).get("rows", [])
    for r in board_rows:
        tr = r.get("tracking", {})
        values = [r.get("name") or "名称未知", r.get("main_plate") or "—", r.get("selection_reason") or "—",
                  f(r.get("auction_pct")), f((r.get("auction_amount_yuan") or 0) / 1e8),
                  f(r.get("p0932_pct")), f(r.get("p0945_pct")), f(tr.get("close_pct")),
                  f(tr.get("delta_close_pp")), r.get("auction_state") or r.get("auction_price_status") or "—"]
        lines.append(" | ".join(str(x) for x in values))
    outcome = data.get("extremes_board", {}).get("close", {})
    for title, key in (("兑现/修复 Top8", "rebound_top8"), ("回落/走弱 Top8", "weak_top8")):
        names = [r.get("name") or "名称未知" for r in outcome.get(key, [])]
        lines.append(f"{title}（按固定竞价集合内Δ排序）：" + ("、".join(names) or "—"))
    lines += ["", "### 旧口径附录：全市场收盘后极值（事后筛选，不是可见机会；Top5）", ""]
    for title, key in (("大肉", "big_meat"), ("大面", "big_loss")):
        lines += [f"### {title}", "", "名称 | 竞价价状态 | 竞价→0932→0945→收盘(%) | Δ/L | 竞价额 | 昨板 | 板块 | 已观测0.5L时点", "---|---|---|---:|---:|---:|---|---"]
        for r in data[key][:5]:
            vals = "→".join(f(r.get(k)) for k in ("auction_pct", "p0932_pct", "p0945_pct", "close_pct"))
            lines.append(f"{r.get('name') or '名称未知'} | {r.get('auction_price_status') or 'UNKNOWN'} | {vals} | {f(r.get('delta_over_limit'))} | {f((r.get('auction_amount_yuan') or 0)/1e8)}亿 | {r.get('lb_days') if r.get('lb_days') is not None else '—'} | {', '.join((r.get('plates') or [])[:2]) or r.get('main_plate') or '—'} | {r['visibility_half_limit']}")
        lines.append("")
    missed = data["missed_opportunities"]
    lines += ["## 漏掉的机会", "", (f"未提示大肉 {missed['count']} 只；个股名称未知；板块计数 {missed['plate_counts']}。" if missed["status"] == "READY" else "无法判断是否已提示：未提供开盘页 JSON。"), "",
              "## 昨日机会回看", ""]
    if data["yesterday_review"]["status"] == "READY":
        for label, p in data["yesterday_review"]["categories"].items():
            lines.append(f"{label}: 今日竞价到收盘 n={p['matched_n']}/{p['expected_n']}，收盘均/中位 {f(p['close_mean'])}%/{f(p['close_median'])}%，大肉/大面 {p['big_meat_count']}/{p['big_loss_count']}。")
    else: lines.append("未提供昨日开盘页名单，跳过。")
    lines += ["", "## 新冒头收盘占比（SCORECARD）", "", "严格新冒头：仅用≤0945检查点，0945涨幅≥5%；收盘涨停按收盘价达到涨停价代理统计，封单状态未知时不称封板。", "", "集合 | n | 收盘≥+5% | 收盘涨停价代理 | 转负", "---|---:|---:|---:|---:"]
    for key, label in (("emerging", "0945新冒头"), ("p0945_top_k", "涨幅榜前K"), ("amount_top_k", "0945累计额前K")):
        p = data["emerging_scorecard"].get(key)
        lines.append(f"{label} | {p['n']} | {p['close_ge_5']} ({pct(p['close_ge_5_rate'])}) | {p['sealed_up']} ({pct(p['sealed_up_rate'])}) | {p['turned_negative']} ({pct(p['turned_negative_rate'])})" if p else f"{label} | — | — | — | —")
    lines += ["", "## 板块", ""]
    for label, key in (("收盘涨幅Top10", "close_top10"), ("收盘涨幅Bottom10", "close_bottom10"), ("竞价额份额Top10（成员重叠，不可相加）", "auction_amount_top10")):
        lines.append(label + ": " + (", ".join(f"{r['name']} {f(r.get('close_mean'))}%" for r in data["plates"][key]) or "数据不可用"))
    deferred = data["plates"].get("deferred_generic_event", [])
    if deferred:
        lines += ["", "<details><summary>泛化/事件板块附录（折叠，不参与主榜）</summary>", "",
                  "；".join(f"{r['name']} ({r.get('kind')}, {r['members']}只成员)" for r in deferred), "", "</details>"]
    lines.append(f"收盘涨幅Top10与竞价额份额Top10重合 {data['plates'].get('top_bottom_overlap') if data['plates'].get('top_bottom_overlap') is not None else '—'} 个。")
    qr = data["plates"].get("quadrant_retention")
    lines.append("Kaipan 强度×主力净额象限保持率：" + "；".join(f"{x['from']}→{x['to']} {x['retained']}/{x['matched']} ({pct(x['rate'])})" for x in qr) if qr else "未提供可用 Kaipan 热板快照，象限延续性不可用。")
    missing_cp = data.get("checkpoint_chapters", {}).get("timeline", {}).get("missing", [])
    cp_note = f"必需检查点缺失 {','.join(missing_cp)}，相关章节已降级且不据此作结论。" if missing_cp else "必需盘中检查点齐全。"
    refs = data.get("reference_data", {})
    ref_note = "参考数据：" + "；".join(f"{name}{'可用' if refs.get(key, {}).get('status') == 'READY' else '缺失'}"
                                         for key, name in (("stock_names", "名称表"), ("stock_caps", "市值表"))) + "。" if refs else ""
    lines += ["", "## 数据质量", "", f"覆盖：收盘 {data['data_quality']['close_pct_coverage']}/{data['data_quality']['included_stock_count']}，0925 {data['data_quality']['auction_pct_coverage']}，0932 {data['data_quality']['p0932_coverage']}，0945 {data['data_quality']['p0945_coverage']}；竞价价 FINAL/INDICATIVE {data['data_quality']['final_count']}/{data['data_quality']['indicative_count']}；首次达到时刻 UNKNOWN（检查点间未观测）。{cp_note}{ref_note}日线来源：{data['data_quality']['daily_source']}。缺失字段保持缺失。", ""]
    return "\n".join(lines)


def render_close_html(data: Mapping[str, Any], markdown: str) -> str:
    # Safe, compact standalone HTML; markdown is escaped as preformatted text.
    return "<!doctype html><meta charset='utf-8'><title>收盘复盘</title><style>body{font:14px system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#222}pre{white-space:pre-wrap;line-height:1.55}</style><pre>" + html.escape(markdown) + "</pre>"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _reference_path(root: Path, key: str, catalog_path: str | None = None) -> Path | None:
    catalog = Path(catalog_path) if catalog_path else root / "reference_catalog.json"
    if not catalog.is_absolute():
        catalog = root / catalog
    try:
        doc = _json(catalog)
        refs = doc.get("references", doc) if isinstance(doc, Mapping) else {}
        value = refs.get(key, {}).get("path") if isinstance(refs, Mapping) else None
        if value:
            candidate = Path(value)
            return candidate if candidate.is_absolute() else root / candidate
    except (OSError, ValueError, AttributeError):
        pass
    matches = sorted(root.rglob("auction_amount_by_day.csv")) if key == "auction_baseline" and root.exists() else []
    return matches[-1] if matches else None


def _close_plate_metadata(relation_dir: str | None) -> tuple[dict[str, dict[str, Any]], str]:
    if not relation_dir:
        return {}, "UNKNOWN"
    base = Path(relation_dir)
    try:
        registry = _json(base / "registry.json")
        plates = registry.get("plates", []) if isinstance(registry, Mapping) else []
        metadata = {str(row.get("name")): {"class": row.get("class"), "size": row.get("size"),
                                            "code": row.get("code")}
                    for row in plates if isinstance(row, Mapping) and row.get("name")}
    except (OSError, ValueError):
        metadata = {}
    match = __import__("re").search(r"20\d{6}", str(base))
    return metadata, (f"{match.group()[:4]}-{match.group()[4:6]}-{match.group()[6:8]}" if match else "UNKNOWN")


def _attach_auction_baseline(data: dict[str, Any], path: Path | None) -> None:
    market = data.get("market")
    if not isinstance(market, dict):
        return
    if not path or not path.is_file():
        gaps = data.setdefault("data_gaps", [])
        if not isinstance(gaps, list):
            gaps = data["data_gaps"] = [str(gaps)]
        gaps.append("竞价额自身历史基线缺失：reference_catalog.auction_baseline 未配置或不可读")
        market["auction_baseline_status"] = "MISSING"
        return
    from .live_session import auction_amount_baseline_comparison, load_auction_amount_baseline_csv
    rows = load_auction_amount_baseline_csv(path, str(data.get("trade_date") or ""))
    amounts = [float(row["auction_amount_yuan"]) for row in data.get("stocks", [])
               if isinstance(row, Mapping) and isinstance(row.get("auction_amount_yuan"), (int, float))]
    if not amounts:
        market["auction_baseline_status"] = "MISSING"
        gaps = data.setdefault("data_gaps", [])
        if not isinstance(gaps, list):
            gaps = data["data_gaps"] = [str(gaps)]
        gaps.append("竞价额自身历史基线缺失：当日没有可用竞价额")
        return
    total_yi = sum(amounts) / 1e8
    comparison = auction_amount_baseline_comparison(rows, str(data.get("trade_date") or ""), total_yi)
    market["auction_baseline_comparison"] = comparison
    market["auction_baseline_status"] = "READY" if comparison else "MISSING"
    market["auction_baseline_source"] = str(path)
    if not comparison:
        gaps = data.setdefault("data_gaps", [])
        if not isinstance(gaps, list):
            gaps = data["data_gaps"] = [str(gaps)]
        gaps.append("竞价额自身历史基线缺失：无有效的历史样本")


def assemble_close_review(trade_date: str, report_dir: str, checkpoints_csv: str, auction_snapshot_csv: str,
                          daily_capture_dir: str, *, daily_history_csv: str | None = None,
                          relation_dir: str | None = None, generic_names_json: str | None = None,
                          stock_caps_json: str | None = None, live_report_json: str | None = None,
                          prev_live_json: str | None = None, history_dir: str | None = None,
                          kaipan_hot_dir: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    extremes, manifest = assemble_from_files(trade_date, report_dir, checkpoints_csv, auction_snapshot_csv,
                                              daily_capture_dir, daily_history_csv, relation_dir, stock_caps_json,
                                              generic_names_json)
    rows = extremes["stocks"]
    checkpoint_rows = _read_csv(Path(checkpoints_csv))
    checkpoint_amounts = {_sym(row.get("symbol")): _num(row.get("amt_yuan_cum"))
                          for row in checkpoint_rows if row.get("tag") == "0945"}
    checkpoint_points = {}
    for point_row in checkpoint_rows:
        symbol = _sym(point_row.get("symbol")); tag = str(point_row.get("tag") or "")
        px, pre = _num(point_row.get("px")), _num(point_row.get("pre_close"))
        if tag in {"0935", "0940"} and px is not None and pre:
            checkpoint_points.setdefault(symbol, {})[tag] = (px / pre - 1) * 100
    for row in rows:
        row["p0945_cumulative_amount_yuan"] = checkpoint_amounts.get(row["symbol"])
        row["p0935_pct"] = checkpoint_points.get(row["symbol"], {}).get("0935")
        row["p0940_pct"] = checkpoint_points.get(row["symbol"], {}).get("0940")
        row["p0945_cumulative_amount_yuan"] = checkpoint_amounts.get(row["symbol"])
    live = _json(Path(live_report_json)) if live_report_json else None
    prev = _json(Path(prev_live_json)) if prev_live_json else None
    daily = load_capture(daily_capture_dir)
    history_for_position = _read_csv(Path(daily_history_csv)) if daily_history_csv else []
    closes_by_symbol: dict[str, list[tuple[str, float]]] = {}
    for item in history_for_position:
        day = item.get("date") or ""
        close = _num(item.get("close"))
        symbol = _sym(item.get("symbol", item.get("code")))
        if day < trade_date and close is not None:
            closes_by_symbol.setdefault(symbol, []).append((day, close))
    for r in rows:
        item = daily.get(r["symbol"], {})
        r["amount_yuan"] = _num(item.get("amount_yuan"))
        history20 = sorted(closes_by_symbol.get(r["symbol"], []))[-20:]
        if len(history20) == 20 and r.get("p0925_pct") is not None:
            prior_close = history20[-1][1]
            low, high = min(value for _, value in history20), max(value for _, value in history20)
            auction_price = prior_close * (1 + r["p0925_pct"] / 100)
            if high > low:
                r["p0925_range_position20_pct"] = (auction_price - low) / (high - low) * 100
    amounts = [_num(r.get("amount_yuan")) for r in daily.values()]
    amounts = [x for x in amounts if x is not None]
    market_closes = []
    market_up_limit = market_down_limit = 0
    for symbol, item in daily.items():
        close, pre = _num(item.get("close")), _num(item.get("pre_close"))
        pct = ((close / pre - 1) * 100) if close is not None and pre else _num(item.get("pct_chg"))
        if pct is None:
            continue
        market_closes.append(pct)
        market_up_limit += is_limit_up(close, pre, symbol, unlimited=bool(item.get("is_unlimited_price_band")))
        market_down_limit += is_limit_down(close, pre, symbol, unlimited=bool(item.get("is_unlimited_price_band")))
    amount = sum(amounts) if amounts else None
    prev_amount = None
    prior_date = None
    historical_amounts: list[tuple[str, float]] = []
    history_rows: list[dict[str, str]] = []
    if daily_history_csv:
        history_rows = _read_csv(Path(daily_history_csv))
        totals_by_day: dict[str, float] = {}
        for row in history_rows:
            day = row.get("date") or ""
            value = _num(row.get("amount_yuan", row.get("amount")))
            if day < trade_date and value is not None:
                totals_by_day[day] = totals_by_day.get(day, 0.0) + value
        historical_amounts = sorted(totals_by_day.items())[-64:]
        prior = [r for r in history_rows if (r.get("date") or "") < trade_date]
        if prior:
            prior_date = max(r.get("date", "") for r in prior)
            prev_amounts = [_num(r.get("amount_yuan", r.get("amount"))) for r in prior if r.get("date") == prior_date]
            prev_amounts = [x for x in prev_amounts if x is not None]
            prev_amount = sum(prev_amounts) if prev_amounts else None
    market = {"amount_yuan": amount, "amount_coverage": len(amounts), "amount_vs_prev": amount / prev_amount if amount is not None and prev_amount else None,
              "previous_amount_date": prior_date, "daily_source": "UNKNOWN", "close_median": _median(market_closes),
              "up_count": sum(x > 0 for x in market_closes), "down_count": sum(x < 0 for x in market_closes),
              "flat_count": sum(x == 0 for x in market_closes), "close_limit_up_count": market_up_limit,
              "close_limit_down_count": market_down_limit, "daily_stock_count": len(daily), "close_coverage": len(market_closes)}
    hist_values = [value for _, value in historical_amounts]
    if hist_values:
        hist_median = _median(hist_values)
        market["amount_64d_median_yuan"] = hist_median
        market["amount_vs_64d_median"] = amount / hist_median if amount is not None and hist_median else None
        market["amount_64d_percentile"] = sum(value <= amount for value in hist_values) / len(hist_values) if amount is not None else None
        market["amount_64d_sample_days"] = len(hist_values)
    if daily_history_csv and daily:
        today_by_symbol = {str(symbol): _num(item.get("amount_yuan")) for symbol, item in daily.items()
                           if _num(item.get("amount_yuan")) is not None}
        prior_day_rows = [row for row in history_rows if (row.get("date") or "") == prior_date]
        prior_by_symbol = {_sym(row.get("symbol", row.get("code"))): _num(row.get("amount_yuan", row.get("amount")))
                           for row in prior_day_rows}
        history_days = sorted({row.get("date", "") for row in history_rows if row.get("date", "") < trade_date})[-64:]
        per_day = {day: {_sym(row.get("symbol", row.get("code"))): _num(row.get("amount_yuan", row.get("amount")))
                         for row in history_rows if row.get("date") == day and _num(row.get("amount_yuan", row.get("amount"))) is not None}
                   for day in history_days}
        fixed = _fixed_common_amounts(today_by_symbol,
                                      {s: v for s, v in prior_by_symbol.items() if v is not None},
                                      list(per_day.values()))
        common_64 = fixed["symbols"]
        today_64 = fixed["today"]
        daily_64 = [sum(values[symbol] for symbol in common_64) for values in per_day.values()]
        median_64 = fixed["history_median"]
        market.update({"amount_common_pool_n": len(common_64), "amount_today_common_yuan": fixed["today"],
                       "amount_prev_common_yuan": fixed["previous"],
                       "amount_vs_prev_common": fixed["today_vs_previous"],
                       "amount_vs_prev_pool": "同64日基准固定共同股票池"})
        market.update({"amount_64d_common_pool_n": len(common_64),
                       "amount_today_64d_common_yuan": today_64,
                       "amount_64d_median_common_yuan": median_64,
                       "amount_vs_64d_median_common": today_64 / median_64 if median_64 else None,
                       "amount_64d_percentile_common": fixed["history_percentile"],
                       "amount_64d_common_pool_days": len(daily_64),
                       "amount_64d_pool": "当日及全部历史日均有数据的固定共同股票池"})
    history = compare_history(extremes, history_dir) if history_dir else None
    dayhist = extremes.get("daily_history")
    if dayhist:
        past_days = [d for d in dayhist.get("days", []) if d.get("trade_date", "") < trade_date][-72:]
        same_regime = [d for d in past_days if d.get("regime") == extremes.get("regime")]
        market["regime_history_sample_days"] = len(past_days)
        market["same_regime_days"] = len(same_regime)
        market["same_regime_close_median"] = _mean([d["summary"].get("market_median") for d in same_regime if d.get("summary", {}).get("market_median") is not None])
        market["regime_history_percentile"] = _rate(sum(d.get("regime") == extremes.get("regime") for d in past_days), len(past_days)) if past_days else None
        if history is None:
            history = {"sample_days": len(past_days), "today_regime": extremes.get("regime"), "same_regime_days": len(same_regime)}
    plates = []
    if relation_dir:
        folder = Path(relation_dir)
        reg = _json(folder / "registry.json"); entries = reg.get("plates", reg) if isinstance(reg, dict) else reg
        if isinstance(entries, dict): entries = [dict(v, code=k) for k, v in entries.items()]
        gen = set()
        gp = Path(generic_names_json) if generic_names_json else None
        if gp and gp.is_file():
            gd = _json(gp); gen = set(gd.get("names", [])) if isinstance(gd, dict) else set(gd)
        plates = [{"code": str(x.get("code", x.get("plateid", ""))), "name": x.get("name"),
                   "kind": x.get("kind", x.get("class")) or classify_plate_kind(str(x.get("name") or "")),
                   "generic": x.get("class") == "GENERIC" or x.get("name") in gen} for x in entries]
        rel = _read_csv(folder / "relation.csv")
        by = {}
        for x in rel: by.setdefault(_sym(x.get("stockid")), []).append(str(x.get("plateid")))
        pnames = {p["code"]: p.get("name") for p in plates}
        counts: dict[str, int] = {}
        for row in rel:
            code = str(row.get("plateid") or "")
            if code:
                counts[code] = counts.get(code, 0) + 1
        for plate in plates:
            plate["members"] = counts.get(plate["code"], 0)
        for r in rows: r["plates"] = [pnames[c] for c in by.get(r["symbol"], []) if pnames.get(c)]
    snapshots = []
    if kaipan_hot_dir:
        for path in sorted(Path(kaipan_hot_dir).glob("hot_*.json")):
            doc = _json(path); resp = doc.get("response", {})
            raw = resp.get("list", []) if isinstance(resp, dict) else []
            parsed = []
            for x in raw:
                if isinstance(x, list) and len(x) > 8:
                    parsed.append({"code": str(x[0]), "name": x[1], "strength": _num(x[2]), "net_yuan": _num(x[6])})
            snapshots.append({"tag": path.stem.removeprefix("hot_"), "rows": parsed})
    result = derive_close_review(rows, trade_date=trade_date, live_report=live, prev_live=prev,
                                 market=market, plates=plates, kaipan_snapshots=snapshots,
                                 regime_history=history,
                                 checkpoint_status={tag: ("READY" if coverage else "MISSING")
                                                    for tag, coverage in manifest.get("checkpoint_coverage", {}).items()})
    capture_meta = _json(Path(daily_capture_dir) / "manifest.json")
    result["data_quality"]["daily_source"] = capture_meta.get("source", "UNKNOWN")
    result["data_quality"]["checkpoint_coverage"] = manifest.get("checkpoint_coverage")
    result["market"]["daily_source"] = capture_meta.get("source", "UNKNOWN")
    manifest = dict(manifest)
    inputs = dict(manifest.get("input_files", {}))
    for label, path in (("live_report", live_report_json), ("prev_live_report", prev_live_json)):
        if path: inputs[str(Path(path))] = {"sha256": _sha(Path(path)), "rows": None}
    if kaipan_hot_dir:
        for path in sorted(Path(kaipan_hot_dir).glob("hot_*.json")):
            inputs[str(path)] = {"sha256": _sha(path), "rows": None}
    manifest.update({"contract": "CloseReviewV1", "trade_date": trade_date, "input_files": inputs,
                     "missing": [k for k, v in (("daily_history_csv", daily_history_csv), ("relation_dir", relation_dir),
                                                  ("live_report_json", live_report_json), ("prev_live_json", prev_live_json),
                                                  ("kaipan_hot_dir", kaipan_hot_dir)) if not v]})
    return result, manifest


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--trade-date", required=True); p.add_argument("--report-dir")
    p.add_argument("--checkpoints-csv"); p.add_argument("--auction-snapshot-csv")
    p.add_argument("--daily-capture-dir"); p.add_argument("--stock-day-facts")
    p.add_argument("--daily-history-csv")
    p.add_argument("--relation-dir"); p.add_argument("--generic-names-json"); p.add_argument("--stock-caps-json")
    p.add_argument("--live-report-json"); p.add_argument("--prev-live-json"); p.add_argument("--history-dir")
    p.add_argument("--kaipan-hot-dir"); p.add_argument("--out-dir", required=True); p.add_argument("--full-json", action="store_true")
    p.add_argument("--reference-root", default="/home/exedev/validation")
    p.add_argument("--reference-catalog")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv); out = Path(args.out_dir)
    if out.exists() and (not out.is_dir() or any(out.iterdir())): raise FileExistsError(f"output directory must be new or empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    if args.stock_day_facts:
        from .stock_day_facts import read_facts
        fact_rows, facts_manifest = read_facts(args.stock_day_facts)
        stocks = []
        for fact in fact_rows:
            pre_close = fact.get("pre_close_milli")
            high_milli, low_milli = fact.get("day_high_milli"), fact.get("day_low_milli")
            high_pct = ((high_milli / pre_close - 1) * 100
                        if isinstance(high_milli, (int, float)) and isinstance(pre_close, (int, float)) and pre_close else None)
            low_pct = ((low_milli / pre_close - 1) * 100
                       if isinstance(low_milli, (int, float)) and isinstance(pre_close, (int, float)) and pre_close else None)
            stocks.append({
                "symbol": fact.get("symbol"), "name": fact.get("name"),
                "close": (fact.get("day_close_milli") / 1000
                          if isinstance(fact.get("day_close_milli"), (int, float)) else None),
                "high": (fact.get("day_high_milli") / 1000
                         if isinstance(fact.get("day_high_milli"), (int, float)) else None),
                "low": (fact.get("day_low_milli") / 1000
                        if isinstance(fact.get("day_low_milli"), (int, float)) else None),
                "pre_close": (fact.get("pre_close_milli") / 1000
                              if isinstance(fact.get("pre_close_milli"), (int, float)) else None),
                "auction_pct": fact.get("p0925_pct"), "p0925_pct": fact.get("p0925_pct"),
                "p0932_pct": fact.get("p0932_pct"), "p0945_pct": fact.get("p0945_pct"),
                "close_pct": fact.get("day_pct"), "high_pct": high_pct, "low_pct": low_pct,
                "auction_amount_yuan": fact.get("auction_match_amount_yuan") or fact.get("p0925_cumulative_amount_yuan"),
                "amount_yuan": fact.get("day_amount_yuan"), "limit_pct": fact.get("limit_pct"),
                "lb_days": fact.get("yesterday_lb_days"),
                "in_yest_pool": fact.get("yesterday_limit_up"),
                "main_plate": fact.get("main_plate_name"), "plates": fact.get("plate_list_summary") or [],
                "auction_price_status": fact.get("p0925_price_status"),
                "auction_limit_state": fact.get("auction_limit_state"),
                "rest_bid_yuan": fact.get("auction_rest_bid_yuan"),
                "close_seal_up": fact.get("close_seal_up"),
                "is_new_listing": fact.get("is_new_listing"),
                "is_unlimited_price_band": bool(fact.get("is_new_listing")),
                "p0925_range_position20_pct": fact.get("position20_0925_pct"),
                "daily_source": fact.get("field_provenance_ref"),
                "caps": {"float_cap": fact.get("float_cap_yuan"), "total_cap": fact.get("total_cap_yuan")},
            })
        # Derive the ladder's dated prior-board counts from the same wide rows;
        # unknown streaks remain excluded from counts and are surfaced below.
        prior_counts: dict[int, int] = {}
        unknown_pool_streaks = 0
        for stock in stocks:
            if stock.get("in_yest_pool"):
                level = stock.get("lb_days")
                if isinstance(level, (int, float)) and level > 0:
                    prior_counts[int(level)] = prior_counts.get(int(level), 0) + 1
                else:
                    unknown_pool_streaks += 1
        ladder_summary = [{"board_height": f"{level}板", "previous_n": count}
                          for level, count in sorted(prior_counts.items())]
        for stock, fact in zip(stocks, fact_rows):
            if fact.get("p0925_pct") is None:
                stock["auction_price_status"] = None
        amount_values = [stock["amount_yuan"] for stock in stocks
                         if isinstance(stock.get("amount_yuan"), (int, float))]
        live_path = Path(args.live_report_json) if args.live_report_json else _reference_path(
            Path(args.reference_root), "live_report", args.reference_catalog)
        prev_path = Path(args.prev_live_json) if args.prev_live_json else _reference_path(
            Path(args.reference_root), "prev_live_report", args.reference_catalog)
        live_report = _json(live_path) if live_path and live_path.is_file() else None
        prev_live = _json(prev_path) if prev_path and prev_path.is_file() else None
        data = derive_close_review(stocks, trade_date=args.trade_date, live_report=live_report,
                                   prev_live=prev_live,
                                   market={"amount_yuan": sum(amount_values) if amount_values else None,
                                           "amount_coverage": len(amount_values),
                                           "daily_source": "STOCK_DAY_FACTS"},
                                   checkpoint_status={tag: "READY" if any(r.get(f"p{tag}_pct") is not None for r in stocks) else "MISSING"
                                                      for tag in ("0932", "0935", "0940", "0945")})
        data["data_quality"]["daily_source"] = "STOCK_DAY_FACTS"
        data["data_quality"]["facts_manifest_sha256"] = facts_manifest.get("sha256")
        if unknown_pool_streaks:
            data.setdefault("data_gaps", []).append(
                f"昨日涨停池连板天数缺失：{unknown_pool_streaks}只，未纳入梯队高度计数")
        manifest = {"contract": "CloseReviewV1", "trade_date": args.trade_date,
                    "facts_source": "STOCK_DAY_FACTS", "facts_manifest": facts_manifest,
                    "legacy_fallback": False,
                    "missing": ["daily_history", *(["live_report"] if live_report is None else []),
                                *(["previous_live_report"] if prev_live is None else []), "Kaipan hot snapshots"]}
    else:
        if not all((args.report_dir, args.checkpoints_csv, args.auction_snapshot_csv, args.daily_capture_dir)):
            raise ValueError("legacy path requires --report-dir, --checkpoints-csv, --auction-snapshot-csv and --daily-capture-dir")
        data, manifest = assemble_close_review(args.trade_date, args.report_dir, args.checkpoints_csv,
            args.auction_snapshot_csv, args.daily_capture_dir, daily_history_csv=args.daily_history_csv,
            relation_dir=args.relation_dir, generic_names_json=args.generic_names_json,
            stock_caps_json=args.stock_caps_json, live_report_json=args.live_report_json,
            prev_live_json=args.prev_live_json, history_dir=args.history_dir, kaipan_hot_dir=args.kaipan_hot_dir)
        manifest["facts_source"] = "LEGACY_FALLBACK"
        manifest["legacy_fallback"] = True
    plate_metadata, relation_asof = _close_plate_metadata(args.relation_dir)
    attach_close_sections(data, relation_asof=relation_asof, plate_metadata=plate_metadata,
                          ladder_summary=ladder_summary if args.stock_day_facts else ())
    _attach_auction_baseline(data, _reference_path(Path(args.reference_root), "auction_baseline",
                                                   args.reference_catalog))
    md = render_close_markdown(data); (out / "report.md").write_text(md, encoding="utf-8")
    (out / "report.html").write_text(render_close_html(data, md), encoding="utf-8")
    compact = {k: v for k, v in data.items() if k not in ("big_meat", "big_loss", "stocks")}
    compact["big_meat"] = [{k: r.get(k) for k in ("symbol", "name", "delta_over_limit", "auction_amount_yuan")} for r in data["big_meat"]]
    compact["big_loss"] = [{k: r.get(k) for k in ("symbol", "name", "delta_over_limit", "auction_amount_yuan")} for r in data["big_loss"]]
    report_json = json.dumps(compact, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    if len(report_json.encode()) > 300_000:
        sizes = sorted(((len(json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode()), k) for k, v in compact.items()), reverse=True)[:6]
        raise ValueError("report.json exceeds 300 KB; largest keys: " + ", ".join(f"{k}={n}" for n, k in sizes))
    (out / "report.json").write_text(report_json, encoding="utf-8")
    if args.full_json: (out / "report.full.json").write_text(json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    manifest["outputs"] = {name: (out / name).stat().st_size for name in ("report.md", "report.html", "report.json")}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    if (out / "report.html").stat().st_size > 150_000: raise ValueError("report.html exceeds 150 KB")
    print(f"trade_date={args.trade_date} stocks={data['data_quality']['included_stock_count']} html_bytes={(out/'report.html').stat().st_size} json_bytes={(out/'report.json').stat().st_size} out={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
