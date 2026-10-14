"""Standalone, post-close auction-to-close extremes review.

All percentages are percentage points. Missing inputs stay missing; they are
never converted to zero. The module is deliberately separate from live reports.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence

from .daily_kline_capture import load_capture

CONTRACT = "ExtremesReviewV1"


def _num(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def limit_percent(symbol: str, name: str = "") -> float | None:
    code = str(symbol or "").upper()
    if code.startswith(("N", "C")) or str(name or "").upper().startswith(("N", "C")):
        return None
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("4", "8", "92")):
        return 30.0
    return 10.0 if code[:1].isdigit() else None


def normalize_extreme(delta_pp: float | None, symbol: str, name: str = "") -> float | None:
    band = limit_percent(symbol, name)
    return None if delta_pp is None or band is None else delta_pp / band


def _pct(row: Mapping[str, Any], field: str, pre_close: float | None) -> float | None:
    direct = _num(row.get(field))
    if direct is not None:
        return direct
    close = _num(row.get("close" if field == "close_pct" else "open"))
    return (close / pre_close - 1) * 100 if close is not None and pre_close else None


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _mean(values: Sequence[float]) -> float | None:
    return statistics.mean(values) if values else None


def _quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _rate(n: int, d: int) -> float | None:
    return n / d if d else None


def _prof(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    vals = [r["delta_over_limit"] for r in rows if r.get("delta_over_limit") is not None]
    closes = [r["close_pct"] for r in rows if r.get("close_pct") is not None]
    return {
        "n": len(vals), "delta_over_limit_mean": _mean(vals), "delta_over_limit_median": _median(vals),
        "big_meat_rate": _rate(sum(x >= .5 for x in vals), len(vals)),
        "big_loss_rate": _rate(sum(x <= -.5 for x in vals), len(vals)),
        "close_green_rate": _rate(sum(x > 0 for x in closes), len(closes)),
    }


def _path_test(row: Mapping[str, Any], upper: float, lower: float) -> dict[str, bool]:
    hi, lo, close = row.get("high_pct"), row.get("low_pct"), row.get("close_pct")
    hit_up = hi is not None and hi >= upper - .2
    hit_down = lo is not None and lo <= -(lower - .2)
    up_close = close is not None and close >= upper - .2
    down_close = close is not None and close <= -(lower - .2)
    return {"hit_up": hit_up, "hit_down": hit_down, "up_close": up_close, "down_close": down_close}


def _bucket(value: float | None, edges: Sequence[tuple[str, float | None, float | None]]) -> str | None:
    if value is None:
        return None
    for label, low, high in edges:
        if (low is None or value >= low) and (high is None or value < high):
            return label
    return edges[-1][0]


def _groups(rows: Sequence[Mapping[str, Any]], key: str, edges: Sequence[tuple[str, float | None, float | None]]) -> dict[str, Any]:
    out = {}
    for row in rows:
        label = _bucket(row.get(key), edges)
        if label:
            out.setdefault(label, []).append(row)
    return {label: _prof(group) for label, group in out.items()}


def derive_extremes_review(rows: Sequence[Mapping[str, Any]], *, trade_date: str,
                           plates: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    stocks = []
    excluded_new = 0
    for source in rows:
        row = dict(source)
        symbol = str(row.get("symbol") or "")
        row["limit_pct"] = _num(row.get("limit_pct")) or limit_percent(symbol, str(row.get("name") or ""))
        if row["limit_pct"] is None:
            excluded_new += 1
            continue
        for f in ("auction_pct", "open_pct", "p0932_pct", "p0945_pct", "close_pct", "high_pct", "low_pct"):
            row[f] = _num(row.get(f))
        if row.get("delta_pp") is None and row.get("auction_pct") is not None and row.get("close_pct") is not None:
            row["delta_pp"] = row["close_pct"] - row["auction_pct"]
        else:
            row["delta_pp"] = _num(row.get("delta_pp"))
        row["delta_over_limit"] = normalize_extreme(row["delta_pp"], symbol, str(row.get("name") or ""))
        row["is_big_meat"] = row["delta_over_limit"] is not None and row["delta_over_limit"] >= .5
        row["is_big_loss"] = row["delta_over_limit"] is not None and row["delta_over_limit"] <= -.5
        row["path_flags"] = _path_test(row, row["limit_pct"], row["limit_pct"])
        stocks.append(row)

    close = [r["close_pct"] for r in stocks if r["close_pct"] is not None]
    delta = [r["delta_pp"] for r in stocks if r["delta_pp"] is not None]
    scaled = [r["delta_over_limit"] for r in stocks if r["delta_over_limit"] is not None]
    left, right = [r for r in stocks if r.get("delta_pp") is not None and r["delta_pp"] <= -8], [r for r in stocks if r.get("delta_pp") is not None and r["delta_pp"] >= 8]
    left_scaled, right_scaled = [r for r in stocks if r.get("delta_over_limit") is not None and r["delta_over_limit"] <= -.5], [r for r in stocks if r.get("delta_over_limit") is not None and r["delta_over_limit"] >= .5]
    pool = [r for r in stocks if r.get("in_yest_pool")]
    paths: dict[str, list[dict[str, Any]]] = {k: [] for k in (
        "地天", "近地天", "天地", "近天地", "竞价封跌", "竞价封涨收盘未封",
        "连板>=3昨日今日断板", "连续跌停后开板", "一字涨停", "一字跌停")}
    for r in stocks:
        f = r["path_flags"]
        if f["hit_down"] and f["up_close"]: paths["地天"].append(r)
        if f["hit_down"] and r.get("close_pct") is not None and r["close_pct"] > 0 and not f["up_close"]: paths["近地天"].append(r)
        if f["hit_up"] and f["down_close"]: paths["天地"].append(r)
        if f["hit_up"] and r.get("close_pct") is not None and r["close_pct"] < 0 and not f["down_close"]: paths["近天地"].append(r)
        if r.get("auction_limit_state") == -1: paths["竞价封跌"].append(r)
        if r.get("auction_limit_state") == 1 and not f["up_close"]: paths["竞价封涨收盘未封"].append(r)
        if (r.get("prior_limit_streak") or r.get("lb_days") or 0) >= 3 and not f["up_close"]: paths["连板>=3昨日今日断板"].append(r)
        if (r.get("prior_down_streak") or 0) >= 2 and not f["down_close"]: paths["连续跌停后开板"].append(r)
        if f["up_close"] and r.get("high_pct") is not None and r["high_pct"] <= r["limit_pct"] + .001: paths["一字涨停"].append(r)
        if f["down_close"] and r.get("low_pct") is not None and r["low_pct"] >= -(r["limit_pct"] + .001): paths["一字跌停"].append(r)

    auction_edges = [("<=-3", None, -3), ("-3~-1", -3, -1), ("-1~+1", -1, 1), ("+1~+3", 1, 3), ("+3~+6", 3, 6), (">=+6", 6, None)]
    rank_edges = [("前100", 1, 101), ("100-500", 101, 501), ("500-1500", 501, 1501), ("1500-3000", 1501, 3001), (">3000", 3001, None)]
    rel_edges = [("<=-2", None, -2), ("-2~+2", -2, 2)]
    move_edges = [("<=-2", None, -2), ("-2~+2", -2, 2), (">=+2", 2, None)]
    binned = {"auction_pct": _groups(stocks, "auction_pct", auction_edges),
              "auction_amount_rank": _groups(stocks, "auction_amount_rank", rank_edges),
              "rel_to_strongest_plate": _groups(stocks, "rel_to_strongest_plate", rel_edges),
              "move_0924_0925_pp": _groups(stocks, "move_0924_0925_pp", move_edges)}
    rel_values = [r for r in stocks if r.get("rel_to_strongest_plate") is not None]
    binned["rel_to_strongest_plate"][">=+2"] = _prof([r for r in rel_values if r["rel_to_strongest_plate"] >= 2])
    binned["rel_to_strongest_plate"][">=+4"] = _prof([r for r in rel_values if r["rel_to_strongest_plate"] >= 4])
    for label, group in (("yest_pool", pool), ("not_yest_pool", [r for r in stocks if not r.get("in_yest_pool")]),
                         ("yest_pool_auction_ge_2", [r for r in pool if r.get("auction_pct") is not None and r["auction_pct"] >= 2])):
        binned[label] = _prof(group)
    baseline = _prof(stocks)
    rel_group = [r for r in stocks if r.get("rel_to_strongest_plate") is not None and r["rel_to_strongest_plate"] >= 2]
    low_group = [r for r in stocks if r.get("auction_pct") is not None and r["auction_pct"] <= -3]

    plate_rows = []
    for plate in plates:
        members = [r for r in stocks if plate.get("name") in (r.get("plates") or [])]
        if len(members) < 8 or plate.get("generic"):
            continue
        pp = _prof(members)
        auc = [r["auction_pct"] for r in members if r.get("auction_pct") is not None]
        cls = [r["close_pct"] for r in members if r.get("close_pct") is not None]
        plate_rows.append({"code": plate.get("code"), "name": plate.get("name"), "members": len(members),
                           **pp, "auction_pct_mean": _mean(auc), "close_pct_mean": _mean(cls),
                           "big_meat_count": sum(bool(r["is_big_meat"]) for r in members),
                           "big_loss_count": sum(bool(r["is_big_loss"]) for r in members)})
    plate_rows.sort(key=lambda r: (r["delta_over_limit_mean"] if r["delta_over_limit_mean"] is not None else -math.inf, r["name"] or ""), reverse=True)
    strongest = sorted(plate_rows, key=lambda r: (r["auction_pct_mean"] if r["auction_pct_mean"] is not None else -math.inf), reverse=True)[:10]
    richest = sorted(plate_rows, key=lambda r: (r["delta_over_limit_mean"] if r["delta_over_limit_mean"] is not None else -math.inf), reverse=True)[:10]
    poorest = sorted(plate_rows, key=lambda r: (r["delta_over_limit_mean"] if r["delta_over_limit_mean"] is not None else math.inf))[:10]
    return {
        "contract": CONTRACT, "trade_date": trade_date, "stocks": stocks,
        "summary": {"n": len(stocks), "close_median": _median(close), "close_mean": _mean(close),
                    "close_up_rate": _rate(sum(x > 0 for x in close), len(close)),
                    "delta_quantiles": {f"p{p}": _quantile(delta, q) for p, q in ((1,.01),(5,.05),(50,.5),(95,.95),(99,.99))},
                    "delta_over_limit_quantiles": {f"p{p}": _quantile(scaled, q) for p, q in ((1,.01),(5,.05),(50,.5),(95,.95),(99,.99))},
                    "left_tail_delta_le_minus_8": len(left), "right_tail_delta_ge_8": len(right),
                    "left_tail_scaled_le_minus_05": len(left_scaled), "right_tail_scaled_ge_05": len(right_scaled),
                    "close_limit_up_count": sum(r["path_flags"]["up_close"] for r in stocks),
                    "close_limit_down_count": sum(r["path_flags"]["down_close"] for r in stocks),
                    "one_word_limit_up_count": sum(r["path_flags"]["up_close"] and r.get("open_pct") is not None and r.get("high_pct") is not None and abs(r["open_pct"] - r["close_pct"]) < .001 and abs(r["high_pct"] - r["close_pct"]) < .001 for r in stocks),
                    "one_word_limit_down_count": sum(r["path_flags"]["down_close"] and r.get("open_pct") is not None and r.get("low_pct") is not None and abs(r["open_pct"] - r["close_pct"]) < .001 and abs(r["low_pct"] - r["close_pct"]) < .001 for r in stocks),
                    "indicative_count": sum(r.get("auction_price_status") == "INDICATIVE" for r in stocks),
                    "excluded_new_listing_count": excluded_new, "yest_pool": _prof(pool),
                    "yest_pool_break_count": sum((r.get("lb_days") or r.get("prior_limit_streak") or 0) > 0 and not r["path_flags"]["up_close"] for r in pool)},
        "big_meat": sorted([r for r in stocks if r["is_big_meat"]], key=lambda r: (-r["delta_over_limit"], r["symbol"]))[:30],
        "big_loss": sorted([r for r in stocks if r["is_big_loss"]], key=lambda r: (r["delta_over_limit"], r["symbol"]))[:30],
        "paths": paths, "bins": binned,
        "diagnostics": {"rel_ge_2_big_loss_rate": _rate(sum(r["delta_over_limit"] is not None and r["delta_over_limit"] <= -.5 for r in rel_group), len(rel_group)),
                        "rel_ge_2_n": len(rel_group), "baseline_big_loss_rate": baseline["big_loss_rate"],
                        "auction_le_minus_3_big_meat_rate": _rate(sum(r["delta_over_limit"] is not None and r["delta_over_limit"] >= .5 for r in low_group), len(low_group)),
                        "auction_le_minus_3_n": len(low_group), "baseline_big_meat_rate": baseline["big_meat_rate"],
                        "four_day_clue_only": True},
        "plates": {"availability": "READY" if plates else "UNAVAILABLE_MISSING_RELATION_DIR",
                   "best_delta_over_limit": richest, "worst_delta_over_limit": poorest,
                   "strongest_auction": strongest,
                   "strongest_close_below_auction_count": sum((r["close_pct_mean"] or 0) < (r["auction_pct_mean"] or 0) for r in strongest)},
    }


def compare_history(today: Mapping[str, Any], history_dir: str | Path | None) -> dict[str, Any]:
    past = []
    if history_dir and Path(history_dir).is_dir():
        for path in sorted(Path(history_dir).glob("????-??-??.json")):
            if path.stem == today.get("trade_date"):
                continue
            try: past.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError): continue
    all_rows = past + [dict(today)]
    rows = []
    for item in all_rows:
        s = item.get("summary", {}); d = item.get("diagnostics", {}); p = item.get("plates", {})
        rows.append({"trade_date": item.get("trade_date"), "market_median": s.get("close_median"),
                     "up_rate": s.get("close_up_rate"), "delta_median": s.get("delta_quantiles", {}).get("p50"),
                     "delta_p5": s.get("delta_quantiles", {}).get("p5"), "delta_p95": s.get("delta_quantiles", {}).get("p95"),
                     "left_tail_count": s.get("left_tail_delta_le_minus_8"), "right_tail_count": s.get("right_tail_delta_ge_8"),
                     "big_meat_count": sum(r.get("is_big_meat", False) for r in item.get("stocks", [])),
                     "big_loss_count": sum(r.get("is_big_loss", False) for r in item.get("stocks", [])),
                     "yest_pool_big_loss_rate": item.get("bins", {}).get("yest_pool", {}).get("big_loss_rate"),
                     "rel_big_loss_rate": d.get("rel_ge_2_big_loss_rate"), "baseline_big_loss_rate": d.get("baseline_big_loss_rate"),
                     "deep_low_big_meat_rate": d.get("auction_le_minus_3_big_meat_rate"), "baseline_big_meat_rate": d.get("baseline_big_meat_rate"),
                     "strongest_plates_mean_delta_l": _mean([x["delta_over_limit_mean"] for x in p.get("strongest_auction", []) if x.get("delta_over_limit_mean") is not None]),
                     "strongest_plates_close_below_auction": p.get("strongest_close_below_auction_count")})
    today_row = next(r for r in rows if r["trade_date"] == today.get("trade_date"))
    value = today_row["left_tail_count"]
    population = [r["left_tail_count"] for r in rows if r["left_tail_count"] is not None]
    today_row["left_tail_historical_percentile"] = _rate(sum(x <= value for x in population), len(population)) if value is not None else None
    return {"sample_days": len(rows), "rows": rows, "today_left_tail_historical_percentile": today_row["left_tail_historical_percentile"]}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _symbol(value: Any) -> str:
    text = str(value or "").strip()
    return text.rsplit(".", 1)[-1].zfill(6) if text.isdigit() else text


def _prior_streaks(rows: Sequence[Mapping[str, Any]], symbol: str, name: str = "") -> tuple[int, int]:
    ordered = sorted(rows, key=lambda row: str(row.get("date") or ""))
    up = down = 0
    band = limit_percent(symbol, name)
    if band is None:
        return 0, 0
    for row in reversed(ordered):
        pc, close = _num(row.get("pre_close")), _num(row.get("close"))
        if not pc or close is None or (close / pc - 1) * 100 < band - .2:
            break
        up += 1
    for row in reversed(ordered):
        pc, close = _num(row.get("pre_close")), _num(row.get("close"))
        if not pc or close is None or (close / pc - 1) * 100 > -(band - .2):
            break
        down += 1
    return up, down


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def assemble_from_files(trade_date: str, report_dir: str | Path, checkpoints_csv: str | Path,
                        auction_snapshot_csv: str | Path, daily_capture_dir: str | Path,
                        daily_history_csv: str | Path | None = None, relation_dir: str | Path | None = None,
                        stock_caps_json: str | Path | None = None, generic_names_json: str | Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    report_path = Path(report_dir)
    stocks_doc = _json(report_path / "stocks.json")
    report_stocks = stocks_doc.get("stocks", stocks_doc) if isinstance(stocks_doc, dict) else stocks_doc
    stocks_by_symbol = {_symbol(r.get("symbol", r.get("code"))): dict(r) for r in report_stocks}
    check_rows = _read_csv(Path(checkpoints_csv)); auction_rows = _read_csv(Path(auction_snapshot_csv))
    checks: dict[str, dict[str, dict[str, str]]] = {}
    for row in check_rows:
        if row.get("date") == trade_date:
            checks.setdefault(_symbol(row.get("symbol")), {})[str(row.get("tag"))] = row
    auctions = {_symbol(r.get("symbol")): r for r in auction_rows if str(r.get("trade_date")) == trade_date and str(r.get("auction_tag")) == "0925"}
    daily = load_capture(daily_capture_dir)
    names = {}
    for r in report_stocks:
        names[_symbol(r.get("symbol", r.get("code")))] = r
    relation: dict[str, list[str]] = {}
    plate_meta: dict[str, dict[str, Any]] = {}
    if relation_dir:
        folder = Path(relation_dir)
        for r in _read_csv(folder / "relation.csv"):
            relation.setdefault(_symbol(r.get("stockid")), []).append(str(r.get("plateid")))
        registry = _json(folder / "registry.json")
        entries = registry.get("plates", registry) if isinstance(registry, dict) else registry
        if isinstance(entries, dict): entries = [dict(v, code=k) for k, v in entries.items()]
        plate_meta = {str(r.get("code", r.get("plateid"))): {"code": str(r.get("code", r.get("plateid"))), "name": r.get("name"), "generic": r.get("class") == "GENERIC"} for r in entries}
    generic_path = Path(generic_names_json) if generic_names_json else Path(__file__).resolve().parents[2] / "examples" / "data" / "plate_generic_factor_names.json"
    if generic_path.is_file():
        generic_doc = _json(generic_path)
        generic_names = set(generic_doc.get("names", ())) if isinstance(generic_doc, dict) else set(generic_doc)
        for plate in plate_meta.values():
            if plate.get("name") in generic_names: plate["generic"] = True
    caps = _json(Path(stock_caps_json)) if stock_caps_json else {}
    rows = []
    for symbol in sorted(set(stocks_by_symbol) | set(daily) | set(checks) | set(auctions)):
        sr = stocks_by_symbol.get(symbol, {}); d = daily.get(symbol, {}); cp = checks.get(symbol, {})
        auc = auctions.get(symbol, {})
        a_pct = _num(auc.get("chg_bp")); a_pct = a_pct / 100 if a_pct is not None else None
        c25 = cp.get("0925", {})
        if a_pct is None:
            px, pc = _num(c25.get("px")), _num(c25.get("pre_close"))
            if px is not None and pc: a_pct = (px / pc - 1) * 100
        last_ts = str(c25.get("last_tick_ts") or "")
        if last_ts.isdigit() and len(last_ts) >= 13:
            from datetime import datetime, timezone, timedelta
            time_text = datetime.fromtimestamp(int(last_ts[:13]) / 1000, timezone(timedelta(hours=8))).strftime("%H:%M:%S")
        else:
            time_text = last_ts.replace("T", " ").split(" ")[-1][:8] if last_ts else ""
        price_status = ("FINAL" if time_text >= "09:25:00" else "INDICATIVE") if time_text and a_pct is not None else "UNKNOWN"
        def point(tag: str) -> float | None:
            row = cp.get(tag, {}); px, pc = _num(row.get("px")), _num(row.get("pre_close"))
            return (px / pc - 1) * 100 if px is not None and pc else None
        pre = _num(d.get("pre_close")); close = _num(d.get("close"))
        openp = _num(d.get("open")); high = _num(d.get("high")); low = _num(d.get("low"))
        def daypct(px: float | None) -> float | None: return (px / pre - 1) * 100 if px is not None and pre else None
        l_pct = limit_percent(symbol, str(sr.get("name") or d.get("name") or ""))
        all_plate_codes = relation.get(symbol, [])
        assigned_plates = [plate_meta[c]["name"] for c in all_plate_codes if c in plate_meta and plate_meta[c].get("name")]
        main_plate = sr.get("primary_plate") or sr.get("plate") or (assigned_plates[0] if assigned_plates else None)
        name = sr.get("name") or d.get("name")
        row = {"symbol": symbol, "name": name, "main_plate": main_plate, "plates": assigned_plates,
               "limit_pct": l_pct, "auction_pct": a_pct, "auction_price_status": price_status,
               "p0932_pct": point("0932"), "p0945_pct": point("0945"), "open_pct": daypct(openp),
               "close_pct": daypct(close), "high_pct": daypct(high), "low_pct": daypct(low),
               "auction_amount_yuan": _num(auc.get("match_amt_yuan")),
               "rest_bid_yuan": _num(auc.get("rest_bid_amt_yuan")), "rest_ask_yuan": _num(auc.get("rest_ask_amt_yuan")),
               "auction_limit_state": int(_num(auc.get("limit_state")) or 0),
               "in_yest_pool": bool(sr.get("in_yest_pool", False)), "lb_days": int(_num(sr.get("lb_days")) or 0),
               "prior_limit_streak": int(_num(sr.get("lb_days")) or 0), "prior_down_streak": 0,
               "caps": caps.get(symbol)}
        rows.append(row)
    # Amount ranks and strongest-plate relative auction strength.
    amount_order = sorted((r for r in rows if r["auction_amount_yuan"] is not None), key=lambda r: (-r["auction_amount_yuan"], r["symbol"]))
    for rank, row in enumerate(amount_order, 1): row["auction_amount_rank"] = rank
    p_auction = {}
    for plate in plate_meta.values():
        members = [r["auction_pct"] for r in rows if plate.get("name") in (r.get("plates") or []) and r.get("auction_pct") is not None]
        if members: p_auction[plate["name"]] = statistics.mean(members)
    for row in rows:
        vals = [p_auction[p] for p in row["plates"] if p in p_auction]
        row["rel_to_strongest_plate"] = row["auction_pct"] - max(vals) if vals and row["auction_pct"] is not None else None
    if daily_history_csv:
        hist = _read_csv(Path(daily_history_csv)); by_code: dict[str, list[dict[str, Any]]] = {}
        for r in hist:
            code = _symbol(r.get("symbol", r.get("code"))); day = str(r.get("date") or "")
            if day < trade_date: by_code.setdefault(code, []).append(r)
        for row in rows:
            streak_up, streak_down = _prior_streaks(by_code.get(row["symbol"], []), row["symbol"], row.get("name") or "")
            row["prior_limit_streak"] = streak_up
            row["prior_down_streak"] = streak_down
    plates = list(plate_meta.values())
    result = derive_extremes_review(rows, trade_date=trade_date, plates=plates)
    paths = [Path(report_path / n) for n in ("stocks.json", "plates.json", "plate_stock_view.json", "manifest.json") if (report_path / n).exists()]
    inputs = [Path(checkpoints_csv), Path(auction_snapshot_csv), Path(daily_capture_dir) / "daily_kline.jsonl", Path(daily_capture_dir) / "manifest.json"] + paths
    if daily_history_csv: inputs.append(Path(daily_history_csv))
    if relation_dir: inputs += [Path(relation_dir) / "relation.csv", Path(relation_dir) / "registry.json"]
    if stock_caps_json: inputs.append(Path(stock_caps_json))
    if generic_path.is_file(): inputs.append(generic_path)
    manifest = {"contract": CONTRACT, "trade_date": trade_date, "input_files": {str(p): {"sha256": _sha(p), "rows": sum(1 for _ in p.open(encoding="utf-8-sig")) - 1 if p.suffix == ".csv" else None} for p in inputs},
                "daily_kline_coverage_td_pool": _rate(sum(s.get("in_yest_pool") and s["symbol"] in daily for s in rows), sum(s.get("in_yest_pool") for s in rows)),
                "checkpoint_coverage": {tag: _rate(sum(tag in checks.get(s, {}) for s in stocks_by_symbol), len(stocks_by_symbol)) for tag in ("0920", "0924", "0925", "0932", "0935", "0940", "0945")},
                "nc_excluded_count": result["summary"]["excluded_new_listing_count"],
                "indicative_count": result["summary"]["indicative_count"]}
    result["manifest"] = manifest
    return result, manifest


def render_extremes_markdown(data: Mapping[str, Any], history: Mapping[str, Any]) -> str:
    s = data["summary"]
    fmt = lambda x, digits=2: "—" if x is None else f"{x:+.{digits}f}"
    lines = [f"# 极值复盘 {data['trade_date']}", "", "## 市场极值总览", "",
             f"样本 {s['n']}；收盘中位/均值 {fmt(s['close_median'])}% / {fmt(s['close_mean'])}%；上涨占比 {fmt((s['close_up_rate'] or 0)*100,1)}%。",
             f"Δ 分位 P1/P5/P50/P95/P99：{', '.join(fmt(s['delta_quantiles'][f'p{x}']) for x in (1,5,50,95,99))} pp。",
             f"Δ/L 分位：{', '.join(fmt(s['delta_over_limit_quantiles'][f'p{x}']) for x in (1,5,50,95,99))}。左/右尾 Δ≤−8/≥+8：{s['left_tail_delta_le_minus_8']}/{s['right_tail_delta_ge_8']}；Δ/L≤−0.5/≥+0.5：{s['left_tail_scaled_le_minus_05']}/{s['right_tail_scaled_ge_05']}；收盘涨/跌停 {s['close_limit_up_count']}/{s['close_limit_down_count']}（一字 {s['one_word_limit_up_count']}/{s['one_word_limit_down_count']}）。",
             f"昨板 n={data['bins']['yest_pool']['n']}，收盘均值 {fmt(data['bins']['yest_pool']['delta_over_limit_mean'])} Δ/L，红盘率 {fmt((data['bins']['yest_pool']['close_green_rate'] or 0)*100,1)}%，断板 {s['yest_pool_break_count']}。", "",
             "## 大肉 / 大面", "", "封停标的竞价不可买入。匹配额单位亿元，未匹配金额单位万元。", ""]
    for title, key in (("大肉前30", "big_meat"), ("大面前30", "big_loss")):
        lines += [f"### {title}", "", "代码 | 名称 | 主板块 | L% | 竞价% | 0932% | 0945% | 收盘% | Δpp | Δ/L | 高/低% | 竞价额亿/名次 | 未匹配买/卖万 | limit_state | 昨板连板 | rel | FINAL状态", "---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---"]
        for r in data[key]:
            lines.append(f"{r['symbol']} | {r.get('name') or '—'} | {r.get('main_plate') or '—'} | {fmt(r.get('limit_pct'))} | {fmt(r.get('auction_pct'))} | {fmt(r.get('p0932_pct'))} | {fmt(r.get('p0945_pct'))} | {fmt(r.get('close_pct'))} | {fmt(r.get('delta_pp'))} | {fmt(r.get('delta_over_limit'))} | {fmt(r.get('high_pct'))}/{fmt(r.get('low_pct'))} | {fmt((r.get('auction_amount_yuan') or 0)/1e8)}/{r.get('auction_amount_rank','—')} | {fmt((r.get('rest_bid_yuan') or 0)/1e4)}/{fmt((r.get('rest_ask_yuan') or 0)/1e4)} | {r.get('auction_limit_state','—')} | {r.get('lb_days',0)} | {fmt(r.get('rel_to_strongest_plate'))} | {r.get('auction_price_status')}")
        lines.append("")
    lines += ["## 极值路径", ""]
    for label, rows in data["paths"].items():
        items = []
        for r in rows:
            item = f"{r['symbol']} {r.get('name') or ''}".strip()
            if label == "竞价封跌": item += f"(0932 {fmt(r.get('p0932_pct'))}%, 0945 {fmt(r.get('p0945_pct'))}%, 收盘 {fmt(r.get('close_pct'))}%, 最低 {fmt(r.get('low_pct'))}%)"
            items.append(item)
        lines.append(f"- {label}: {len(rows)} — " + (", ".join(items) if items else "当日无"))
    lines += ["", "## 分档", ""]
    for group_name, groups in data["bins"].items():
        if not isinstance(groups, dict) or "n" in groups: continue
        lines += [f"### {group_name}", "", "档 | n | Δ/L均值 | 中位 | 大肉率 | 大面率 | 红盘率", "---|---:|---:|---:|---:|---:|---:"]
        for label, p in groups.items(): lines.append(f"{label} | {p['n']} | {fmt(p['delta_over_limit_mean'])} | {fmt(p['delta_over_limit_median'])} | {fmt((p['big_meat_rate'] or 0)*100,1)}% | {fmt((p['big_loss_rate'] or 0)*100,1)}% | {fmt((p['close_green_rate'] or 0)*100,1)}%")
        lines.append("")
    lines += [f"抢跑代理(rel≥+2)大面率 {fmt((data['diagnostics']['rel_ge_2_big_loss_rate'] or 0)*100,1)}% vs 基线 {fmt((data['diagnostics']['baseline_big_loss_rate'] or 0)*100,1)}%；深度低开(竞价≤−3)大肉率 {fmt((data['diagnostics']['auction_le_minus_3_big_meat_rate'] or 0)*100,1)}% vs 基线 {fmt((data['diagnostics']['baseline_big_meat_rate'] or 0)*100,1)}%。4 日线索，未证明稳健。", "",
             "## 板块聚合", "", ("成员数≥8 的非泛化板块：" if data["plates"].get("availability") == "READY" else "缺少 --relation-dir，无法计算成员级板块聚合；不是零个板块。"),
             (f"竞价最强10板块中收盘弱于竞价 {data['plates']['strongest_close_below_auction_count']} 个。" if data["plates"].get("availability") == "READY" else ""), "",
             "板块 | 成员 | Δ/L均值 | 竞价均值 | 收盘均值 | 大肉/大面", "---|---:|---:|---:|---:|---:"]
    for title, key in (("Δ/L最高", "best_delta_over_limit"), ("Δ/L最低", "worst_delta_over_limit"), ("竞价最强", "strongest_auction")):
        for r in data["plates"][key]: lines.append(f"{title} {r.get('name') or r.get('code')} | {r['members']} | {fmt(r['delta_over_limit_mean'])} | {fmt(r['auction_pct_mean'])} | {fmt(r['close_pct_mean'])} | {r['big_meat_count']}/{r['big_loss_count']}")
    lines += ["", 
             "## 多日对比", "", f"历史样本 {history['sample_days']} 日；今日左尾数历史分位 {fmt((history['today_left_tail_historical_percentile'] or 0)*100,1)}%。", "",
             "日期 | 市场中位 | 上涨占比 | Δ中位 | ΔP5 | ΔP95 | 左/右尾 | 大肉/大面 | 昨板大面率 | 抢跑大面/基线 | 深低开大肉/基线 | 最强10板块Δ/L均值 | 收盘低于竞价", "---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:"]
    for r in history["rows"]:
        lines.append(f"{r['trade_date']} | {fmt(r['market_median'])} | {fmt((r['up_rate'] or 0)*100,1)}% | {fmt(r['delta_median'])} | {fmt(r['delta_p5'])} | {fmt(r['delta_p95'])} | {r['left_tail_count']}/{r['right_tail_count']} | {r['big_meat_count']}/{r['big_loss_count']} | {fmt((r['yest_pool_big_loss_rate'] or 0)*100,1)}% | {fmt((r['rel_big_loss_rate'] or 0)*100,1)}%/{fmt((r['baseline_big_loss_rate'] or 0)*100,1)}% | {fmt((r['deep_low_big_meat_rate'] or 0)*100,1)}%/{fmt((r['baseline_big_meat_rate'] or 0)*100,1)}% | {fmt(r['strongest_plates_mean_delta_l'])} | {r['strongest_plates_close_below_auction']}")
    lines += ["", "## 口径说明", "", "Δ=收盘涨跌幅−0925涨跌幅；Δ/L 以板块涨跌停幅度标准化。竞价涨跌幅取 0925；FINAL 表示最后 tick 不早于09:25:00，INDICATIVE 为更早指示价。竞价为虚拟撮合，无成交量额；未匹配买卖为二档。涨停需有封单，竞价 limit_state 表示封停。没有逐笔委托，抢跑/抢筹仅为聚合推断。此报告为事后复盘，不是预测；分档阈值仅用于展示。收盘数据只用于本独立复盘。", ""]
    return "\n".join(lines)


__all__ = ["CONTRACT", "limit_percent", "normalize_extreme", "derive_extremes_review", "compare_history", "assemble_from_files", "render_extremes_markdown"]
