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


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _mean(values: Sequence[float]) -> float | None:
    return statistics.mean(values) if values else None


def _rate(n: int, d: int) -> float | None:
    return n / d if d else None


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
                        regime_history: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Derive compact close-review sections from normalized per-stock observations."""
    clean = []
    for source in rows:
        r = dict(source)
        r["symbol"] = _sym(r.get("symbol", r.get("code")))
        r.setdefault("limit_pct", limit_percent(r["symbol"], str(r.get("name") or "")))
        r["auction_pct"] = _num(r.get("auction_pct")); r["close_pct"] = _num(r.get("close_pct"))
        if r.get("delta_pp") is None and r["auction_pct"] is not None and r["close_pct"] is not None:
            r["delta_pp"] = r["close_pct"] - r["auction_pct"]
        r["delta_pp"] = _num(r.get("delta_pp"))
        band = _num(r.get("limit_pct"))
        r["delta_over_limit"] = r["delta_pp"] / band if r["delta_pp"] is not None and band else None
        r["is_big_meat"] = r["delta_over_limit"] is not None and r["delta_over_limit"] >= .5
        r["is_big_loss"] = r["delta_over_limit"] is not None and r["delta_over_limit"] <= -.5
        r["close_at_limit_up"] = band is not None and r["close_pct"] is not None and r["close_pct"] >= band - .2
        p932, p945 = _num(r.get("p0932_pct")), _num(r.get("p0945_pct"))
        r["visibility"] = "0945前可见" if (p932 is not None and p932 >= 2) or (p945 is not None and p945 >= 2) else "盘中才出现"
        clean.append(r)
    base = derive_extremes_review(clean, trade_date=trade_date, plates=plates or ())
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

    emerging_ids = sorted(_listed_symbols((live_report or {}).get("chapters", {}).get("emerging"))) if live_report else []
    by_symbol = {r["symbol"]: r for r in clean}
    emerging_rows = [by_symbol[s] for s in emerging_ids if s in by_symbol]
    k = len(emerging_ids)
    def score(group: Sequence[Mapping[str, Any]], expected: int | None = None) -> dict[str, Any]:
        n = len(group)
        return {"n": n if expected is None else expected, "matched": n,
                "close_ge_5": sum(r.get("close_pct") is not None and r["close_pct"] >= 5 for r in group),
                "sealed_up": sum(r.get("close_at_limit_up", False) for r in group),
                "turned_negative": sum(r.get("close_pct") is not None and r["close_pct"] < 0 for r in group),
                "close_ge_5_rate": _rate(sum(r.get("close_pct") is not None and r["close_pct"] >= 5 for r in group), n),
                "sealed_up_rate": _rate(sum(r.get("close_at_limit_up", False) for r in group), n),
                "turned_negative_rate": _rate(sum(r.get("close_pct") is not None and r["close_pct"] < 0 for r in group), n)}
    p945_ranked = sorted((r for r in clean if r.get("p0945_pct") is not None), key=lambda r: (-r["p0945_pct"], r["symbol"]))[:k]
    amount_ranked = sorted((r for r in clean if r.get("auction_amount_yuan") is not None), key=lambda r: (-r["auction_amount_yuan"], r["symbol"]))[:k]
    scorecard = {"status": "READY" if live_report else "UNAVAILABLE_NO_LIVE_REPORT",
                 "emerging": score(emerging_rows, k) if live_report else None,
                 "p0945_top_k": score(p945_ranked) if live_report else None,
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
    for plate in plates or ():
        name = plate.get("name") or plate.get("code")
        members = [r for r in clean if name in (r.get("plates") or [])]
        closes = [r["close_pct"] for r in members if r.get("close_pct") is not None]
        amounts = [r["auction_amount_yuan"] for r in members if r.get("auction_amount_yuan") is not None]
        if len(members) >= 8 and not plate.get("generic") and closes:
            plate_rows.append({"name": name, "members": len(members), "close_mean": _mean(closes),
                               "auction_amount_yuan": sum(amounts) if amounts else None})
    total_plate_amt = sum(p["auction_amount_yuan"] for p in plate_rows if p["auction_amount_yuan"] is not None)
    for p in plate_rows:
        p["auction_amount_share"] = _rate(p["auction_amount_yuan"], total_plate_amt) if p["auction_amount_yuan"] is not None else None
    top_amt = sorted((p for p in plate_rows if p["auction_amount_share"] is not None), key=lambda p: (-p["auction_amount_share"], p["name"]))[:10]
    plate_rows.sort(key=lambda p: (p["close_mean"], p["name"]))
    plate_section = {"status": "READY" if plates else "UNAVAILABLE_NO_RELATION",
                     "close_top10": list(reversed(plate_rows[-10:])), "close_bottom10": plate_rows[:10],
                     "auction_amount_top10": top_amt,
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

    return {"contract": "CloseReviewV1", "trade_date": trade_date, "market": market_section,
            "big_meat": meat, "big_loss": loss, "missed_opportunities": missed_section,
            "yesterday_review": yesterday_section, "emerging_scorecard": scorecard,
            "plates": plate_section, "regime_history": regime_history,
            "stocks": clean,
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
             f"收盘中位 {f(m.get('close_median'))}%；涨/跌/平 {m.get('up_count','—')}/{m.get('down_count','—')}/{m.get('flat_count','—')}；收盘涨停/跌停 {m.get('close_limit_up_count','—')}/{m.get('close_limit_down_count','—')}；全天成交额 {f(m.get('amount_yuan'))} 元；前日倍数 {f(m.get('amount_vs_prev'))}×。",
             f"日型 {m.get('regime_label','—')}；{m.get('auction_comparison','竞价日型/金额对照缺失')}。", "", "## 大肉 / 大面（竞价额降序前20）", ""]
    for title, key in (("大肉", "big_meat"), ("大面", "big_loss")):
        lines += [f"### {title}", "", "代码 | 名称 | 竞价→0932→0945→收盘(%) | Δ/L | 竞价额 | 昨板 | 板块 | 0945前可见", "---|---|---:|---:|---:|---:|---|---"]
        for r in data[key]:
            vals = "→".join(f(r.get(k)) for k in ("auction_pct", "p0932_pct", "p0945_pct", "close_pct"))
            lines.append(f"{r['symbol']} | {r.get('name') or '—'} | {vals} | {f(r.get('delta_over_limit'))} | {f((r.get('auction_amount_yuan') or 0)/1e8)}亿 | {r.get('lb_days',0)} | {', '.join((r.get('plates') or [])[:2]) or r.get('main_plate') or '—'} | {r['visibility']}")
        lines.append("")
    missed = data["missed_opportunities"]
    lines += ["## 漏掉的机会", "", (f"未提示大肉 {missed['count']} 只：{', '.join(missed['symbols'] or [])}；板块计数 {missed['plate_counts']}。" if missed["status"] == "READY" else "无法判断是否已提示：未提供开盘页 JSON。"), "",
              "## 昨日机会回看", ""]
    if data["yesterday_review"]["status"] == "READY":
        for label, p in data["yesterday_review"]["categories"].items():
            lines.append(f"{label}: 今日竞价到收盘 n={p['matched_n']}/{p['expected_n']}，收盘均/中位 {f(p['close_mean'])}%/{f(p['close_median'])}%，大肉/大面 {p['big_meat_count']}/{p['big_loss_count']}。")
    else: lines.append("未提供昨日开盘页名单，跳过。")
    lines += ["", "## 新冒头收盘占比（SCORECARD）", "", "收盘涨停按日线收盘价达到涨停价计（没有收盘盘口封单数据时不代表封单量已核实）。", "", "集合 | n | 收盘≥+5% | 收盘涨停价 | 转负", "---|---:|---:|---:|---:"]
    for key, label in (("emerging", "0945新冒头"), ("p0945_top_k", "涨幅榜前K"), ("amount_top_k", "额榜前K")):
        p = data["emerging_scorecard"].get(key)
        lines.append(f"{label} | {p['n']} | {p['close_ge_5']} ({pct(p['close_ge_5_rate'])}) | {p['sealed_up']} ({pct(p['sealed_up_rate'])}) | {p['turned_negative']} ({pct(p['turned_negative_rate'])})" if p else f"{label} | — | — | — | —")
    lines += ["", "## 板块", ""]
    for label, key in (("收盘涨幅Top10", "close_top10"), ("收盘涨幅Bottom10", "close_bottom10"), ("竞价额份额Top10", "auction_amount_top10")):
        lines.append(label + ": " + (", ".join(f"{r['name']} {f(r.get('close_mean'))}%" for r in data["plates"][key]) or "数据不可用"))
    lines.append(f"收盘涨幅Top10与竞价额份额Top10重合 {data['plates'].get('top_bottom_overlap') if data['plates'].get('top_bottom_overlap') is not None else '—'} 个。")
    qr = data["plates"].get("quadrant_retention")
    lines.append("Kaipan 强度×主力净额象限保持率：" + "；".join(f"{x['from']}→{x['to']} {x['retained']}/{x['matched']} ({pct(x['rate'])})" for x in qr) if qr else "未提供可用 Kaipan 热板快照，象限延续性不可用。")
    lines += ["", "## 数据质量", "", f"覆盖：收盘 {data['data_quality']['close_pct_coverage']}/{data['data_quality']['included_stock_count']}，0925 {data['data_quality']['auction_pct_coverage']}，0932 {data['data_quality']['p0932_coverage']}，0945 {data['data_quality']['p0945_coverage']}；竞价 FINAL/INDICATIVE {data['data_quality']['final_count']}/{data['data_quality']['indicative_count']}。日线来源：{data['data_quality']['daily_source']}。缺失字段保持缺失。", ""]
    return "\n".join(lines)


def render_close_html(data: Mapping[str, Any], markdown: str) -> str:
    # Safe, compact standalone HTML; markdown is escaped as preformatted text.
    return "<!doctype html><meta charset='utf-8'><title>收盘复盘</title><style>body{font:14px system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#222}pre{white-space:pre-wrap;line-height:1.55}</style><pre>" + html.escape(markdown) + "</pre>"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


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
    live = _json(Path(live_report_json)) if live_report_json else None
    prev = _json(Path(prev_live_json)) if prev_live_json else None
    daily = load_capture(daily_capture_dir)
    for r in rows:
        item = daily.get(r["symbol"], {})
        r["amount_yuan"] = _num(item.get("amount_yuan"))
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
        band = limit_percent(symbol, str(item.get("name") or ""))
        if band is not None:
            market_up_limit += pct >= band - .2
            market_down_limit += pct <= -(band - .2)
    amount = sum(amounts) if amounts else None
    prev_amount = None
    prior_date = None
    if daily_history_csv:
        history_rows = _read_csv(Path(daily_history_csv))
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
                   "generic": x.get("class") == "GENERIC" or x.get("name") in gen} for x in entries]
        rel = _read_csv(folder / "relation.csv")
        by = {}
        for x in rel: by.setdefault(_sym(x.get("stockid")), []).append(str(x.get("plateid")))
        pnames = {p["code"]: p.get("name") for p in plates}
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
                                 regime_history=history)
    capture_meta = _json(Path(daily_capture_dir) / "manifest.json")
    result["data_quality"]["daily_source"] = capture_meta.get("source", "UNKNOWN")
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
    p.add_argument("--trade-date", required=True); p.add_argument("--report-dir", required=True)
    p.add_argument("--checkpoints-csv", required=True); p.add_argument("--auction-snapshot-csv", required=True)
    p.add_argument("--daily-capture-dir", required=True); p.add_argument("--daily-history-csv")
    p.add_argument("--relation-dir"); p.add_argument("--generic-names-json"); p.add_argument("--stock-caps-json")
    p.add_argument("--live-report-json"); p.add_argument("--prev-live-json"); p.add_argument("--history-dir")
    p.add_argument("--kaipan-hot-dir"); p.add_argument("--out-dir", required=True); p.add_argument("--full-json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv); out = Path(args.out_dir)
    if out.exists() and (not out.is_dir() or any(out.iterdir())): raise FileExistsError(f"output directory must be new or empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    data, manifest = assemble_close_review(args.trade_date, args.report_dir, args.checkpoints_csv,
        args.auction_snapshot_csv, args.daily_capture_dir, daily_history_csv=args.daily_history_csv,
        relation_dir=args.relation_dir, generic_names_json=args.generic_names_json,
        stock_caps_json=args.stock_caps_json, live_report_json=args.live_report_json,
        prev_live_json=args.prev_live_json, history_dir=args.history_dir, kaipan_hot_dir=args.kaipan_hot_dir)
    md = render_close_markdown(data); (out / "report.md").write_text(md, encoding="utf-8")
    (out / "report.html").write_text(render_close_html(data, md), encoding="utf-8")
    compact = {k: v for k, v in data.items() if k not in ("big_meat", "big_loss", "stocks")}
    compact["big_meat"] = [{k: r.get(k) for k in ("symbol", "name", "delta_over_limit", "auction_amount_yuan")} for r in data["big_meat"]]
    compact["big_loss"] = [{k: r.get(k) for k in ("symbol", "name", "delta_over_limit", "auction_amount_yuan")} for r in data["big_loss"]]
    report_json = json.dumps(compact, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    if len(report_json.encode()) > 300_000: raise ValueError("report.json exceeds 300 KB")
    (out / "report.json").write_text(report_json, encoding="utf-8")
    if args.full_json: (out / "report.full.json").write_text(json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    manifest["outputs"] = {name: (out / name).stat().st_size for name in ("report.md", "report.html", "report.json")}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    if (out / "report.html").stat().st_size > 150_000: raise ValueError("report.html exceeds 150 KB")
    print(f"trade_date={args.trade_date} stocks={data['data_quality']['included_stock_count']} html_bytes={(out/'report.html').stat().st_size} json_bytes={(out/'report.json').stat().st_size} out={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
