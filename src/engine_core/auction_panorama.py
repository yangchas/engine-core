"""Pure V6 09:26 auction panorama data product (no presentation rendering)."""
from __future__ import annotations

import json
import math
import os
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .data_pipeline.storage import ArtifactCatalog

TZ = ZoneInfo("Asia/Shanghai")
SCHEMA = "auction_panorama_v1"


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _median(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _symbol(value: Any) -> str:
    raw = str(value or "").strip()
    digits = "".join(c for c in raw if c.isdigit())
    return digits[-6:].zfill(6) if digits else raw


def _members(rows: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if isinstance(rows, Mapping):
        for code, values in rows.items():
            if isinstance(values, Mapping):
                name = values.get("plate_name", values.get("name", code))
                symbols = values.get("members", values.get("symbols", []))
            else:
                name, symbols = code, values
            for symbol in symbols or []:
                s = _symbol(symbol.get("symbol") if isinstance(symbol, Mapping) else symbol)
                if s:
                    result.setdefault(str(code), {"plate_id": str(code), "plate_name": str(name), "symbols": set()})["symbols"].add(s)
    else:
        for row in rows or []:
            if not isinstance(row, Mapping):
                continue
            code = str(row.get("plate_id", row.get("plate_code", row.get("plateid", row.get("plate", "")))) or "")
            symbol = _symbol(row.get("symbol", row.get("stock_code")))
            if code and symbol:
                entry = result.setdefault(code, {"plate_id": code,
                                                  "plate_name": str(row.get("plate_name", row.get("name", code))),
                                                  "symbols": set()})
                entry["symbols"].add(symbol)
                if row.get("plate_name"):
                    entry["plate_name"] = str(row["plate_name"])
    return result


def _primary_reason(pool_row: Mapping[str, Any], historical: Any) -> str:
    """Priority: explicit yesterday-pool label, then first D-1 history tag, then static tag."""
    for key in ("pool_reason", "primary_reason", "reason_sclt", "reason"):
        value = pool_row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    assignment = historical if isinstance(historical, Mapping) else {}
    if assignment.get("pool_label") not in (None, ""):
        return str(assignment["pool_label"]).strip()
    historical = assignment.get("reasons", assignment.get("tags", historical))
    if isinstance(historical, str):
        historical = [historical]
    if historical:
        return sorted({str(value).strip() for value in historical if value not in (None, "")})[0]
    if assignment.get("static") not in (None, ""):
        return str(assignment["static"]).strip()
    for key in ("static_reason", "static_tag", "theme"):
        if pool_row.get(key) not in (None, ""):
            return str(pool_row[key]).strip()
    return "UNKNOWN"


def _rank(rows: Sequence[Mapping[str, Any]], value_fn) -> dict[str, int]:
    values = [(str(row["symbol"]), value_fn(row)) for row in rows]
    values = [(symbol, value) for symbol, value in values if value is not None]
    return {symbol: 1 + sum(other > value for _s, other in values) for symbol, value in values}


def normalize_hot_plates(rows: Sequence[Mapping[str, Any]], *, fetched_at: str,
                         available_at: str | None = None, asof: str, trade_date: str,
                         now: str | None = None) -> dict[str, Any]:
    """Return code-keyed hot rank facts; ties share rank, ordered by code for display."""
    fetch_dt = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    cutoff = datetime.fromisoformat(asof.replace("Z", "+00:00"))
    current_date = None
    if now:
        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        current_date = now_dt.date().isoformat()
    available_dt = (datetime.fromisoformat(available_at.replace("Z", "+00:00"))
                    if available_at else None)
    posthoc_dev = os.getenv("POSTHOC_DEV", "").strip().lower() in {"1", "true", "yes"}
    historical_replay = bool(current_date and trade_date < current_date and posthoc_dev)
    if available_dt is None or available_dt.tzinfo is None or fetch_dt.tzinfo is None:
        return {"basis": "UNKNOWN", "ranks": {}, "coverage": "0/0"}
    if fetch_dt > cutoff or available_dt > cutoff:
        if not historical_replay:
            return {"basis": "UNKNOWN", "ranks": {}, "coverage": "0/0"}
        basis = "POSTHOC_DEV"
    else:
        basis = "PIT_LIVE"
    valid = [r for r in rows if isinstance(r, Mapping)
             and r.get("plate_code", r.get("code")) is not None
             and isinstance(r.get("strength"), (int, float))]
    valid.sort(key=lambda r: (-float(r["strength"]), str(r.get("plate_code", r.get("code")))))
    ranks = {}
    previous = object(); rank = 0
    for index, row in enumerate(valid, 1):
        score = float(row["strength"])
        if score != previous: rank = index; previous = score
        ranks[str(row.get("plate_code", row.get("code")))] = {"hot_rank": rank, "strength": score,
              "change_pct": row.get("change_pct")}
    return {"basis": basis, "ranks": ranks, "coverage": f"{len(ranks)}/{len(valid)}"}


def build_auction_panorama(stock_facts: Mapping[str, Any], *,
                           plate_members: Any,
                           yesterday_pool: Sequence[Mapping[str, Any]],
                           yesterday_reasons: Mapping[str, Any] | None = None,
                           theme_assignments: Mapping[str, Mapping[str, Any]] | None = None,
                           name_map: Mapping[str, Any] | None = None,
                           streak_feedback: Mapping[str, Any] | None = None,
                           market_amount_history: Sequence[Any] | Mapping[str, Any] | None = None,
                           hot_plate_ranks: Mapping[str, Any] | None = None,
                           plate_classes: Mapping[str, str] | None = None,
                           amount_baseline_basis: str = "PIT") -> dict[str, Any]:
    """Build the schema-versioned facts. All monetary values are yuan; pct_change is percent."""
    trade_date = str(stock_facts.get("trade_date", ""))
    asof = str(stock_facts.get("asof", ""))
    cutoff = datetime.fromisoformat(asof.replace("Z", "+00:00"))
    if cutoff.tzinfo is None or cutoff.date().isoformat() != trade_date or cutoff.timetz().replace(tzinfo=None).isoformat() > "09:26:00":
        raise ValueError("invalid_panorama_asof")
    source_rows = [dict(row) for row in stock_facts.get("rows", []) if isinstance(row, Mapping)]
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in source_rows:
        symbol = _symbol(row.get("symbol"))
        if symbol:
            row["symbol"] = symbol
            by_symbol[symbol] = row
    normalized_names = {_symbol(symbol): (value.get("name") if isinstance(value, Mapping) else value)
                        for symbol, value in (name_map or {}).items()}
    def stock_name(symbol: str, row: Mapping[str, Any] | None = None,
                   pool_row: Mapping[str, Any] | None = None) -> Any:
        return ((pool_row or {}).get("name") or (row or {}).get("name")
                or normalized_names.get(symbol))
    amount_ranks = {s: row.get("amount_rank") for s, row in by_symbol.items()}
    if not any(value is not None for value in amount_ranks.values()):
        amount_ranks = _rank(list(by_symbol.values()), lambda row: _num(row.get("auction_amount_yuan")))
    seals = {s: row.get("seal", {}) for s, row in by_symbol.items()}
    seal_ranks = {s: seals[s].get("seal_rank") for s in by_symbol}
    if not any(value is not None for value in seal_ranks.values()):
        seal_ranks = _rank(list(by_symbol.values()), lambda row: _num(row.get("seal", {}).get("seal_amount_yuan"))
                            if row.get("seal", {}).get("status") in ("VERIFIED", "VERIFIED_REPLAY") else None)
    amounts = [_num(row.get("auction_amount_yuan")) for row in by_symbol.values()]
    valid_amounts = [value for value in amounts if value is not None and value > 0]
    market_amount = sum(valid_amounts) if valid_amounts else None
    price_rows = [row for row in by_symbol.values() if row.get("valid_price") and _num(row.get("pct_change")) is not None]
    changes = [_num(row.get("pct_change")) for row in price_rows]
    changes = [value for value in changes if value is not None]
    market_median = _median(changes)
    up_n = sum(value > 0 for value in changes)
    seals_verified = [row for row in by_symbol.values() if row.get("seal", {}).get("status") in ("VERIFIED", "VERIFIED_REPLAY")
                      and _num(row.get("seal", {}).get("seal_amount_yuan")) is not None]
    verified_sum = sum(_num(row["seal"]["seal_amount_yuan"]) for row in seals_verified)
    candidates = int(stock_facts.get("summary", {}).get("limit_candidate_n",
                   sum(bool(row.get("limit_up_candidate")) for row in by_symbol.values())) or 0)
    if isinstance(market_amount_history, Mapping):
        history = market_amount_history.get("values", market_amount_history.get("amounts", []))
        baseline_status = market_amount_history.get("status", "UNKNOWN")
    else:
        history, baseline_status = market_amount_history or [], "READY"
    history_values = [_num(value.get("amount_yuan") if isinstance(value, Mapping) else value) for value in history]
    history_values = [value for value in history_values if value is not None and value > 0]
    history_values = history_values[-64:]
    multiple = market_amount / statistics.median(history_values) if market_amount is not None and history_values else None
    percentile = (sum(value <= market_amount for value in history_values) / len(history_values)
                  if market_amount is not None and history_values else None)

    pool_by_symbol: dict[str, dict[str, Any]] = {}
    for raw in yesterday_pool:
        if isinstance(raw, Mapping) and (symbol := _symbol(raw.get("symbol", raw.get("code")))):
            pool_by_symbol[symbol] = {**dict(raw), "symbol": symbol}
    pool_price_rows = [by_symbol[s] for s in pool_by_symbol if s in by_symbol and by_symbol[s].get("valid_price")
                       and _num(by_symbol[s].get("pct_change")) is not None]
    pool_changes = [_num(row.get("pct_change")) for row in pool_price_rows]
    pool_up = sum(value > 0 for value in pool_changes)
    pool_open = {"numerator": pool_up, "denominator": len(pool_changes),
                 "ratio": pool_up / len(pool_changes) if pool_changes else None,
                 "valid_price_n": len(pool_changes), "pool_n": len(pool_by_symbol)}
    market_up_ratio = up_n / len(changes) if changes else None
    high_open_pp = (pool_open["ratio"] - market_up_ratio) * 100 if pool_open["ratio"] is not None and market_up_ratio is not None else None

    plate_map = _members(plate_members)
    plate_stats = []
    plate_classes = plate_classes or {}
    for code, plate in plate_map.items():
        members = [by_symbol[s] for s in sorted(plate["symbols"]) if s in by_symbol]
        valid = [row for row in members if row.get("valid_price") and _num(row.get("pct_change")) is not None]
        pcts = [_num(row.get("pct_change")) for row in valid]
        pcts = [v for v in pcts if v is not None]
        size = len(plate["symbols"])
        scale = "小" if 5 <= size <= 19 else "中" if 20 <= size <= 99 else "大" if size >= 100 else None
        member_amount = [_num(row.get("auction_amount_yuan")) for row in members]
        member_amount = [v for v in member_amount if v is not None and v > 0]
        total = sum(member_amount) if member_amount else None
        ordered_amount = sorted((( _num(row.get("auction_amount_yuan")), row) for row in members
                                 if _num(row.get("auction_amount_yuan")) is not None), key=lambda x: (-x[0], x[1]["symbol"]))
        up = sum(value > 0 for value in pcts)
        plate_stats.append({"plate_id": code, "plate_name": plate["plate_name"], "count": size,
                            "class": plate_classes.get(code, "UNKNOWN"),
                            "scale": scale, "amount_yuan": total,
                            "market_share": total / market_amount if total is not None and market_amount else None,
                            "member_median_pct": _median(pcts), "equal_weight_pct": _median(pcts),
                            "up_count": up, "valid_price_count": len(pcts),
                            "strength_vs_market_pct": _median(pcts) - market_median if pcts and market_median is not None else None,
                            "up_ratio": up / len(pcts) if pcts else None,
                            "top1_share": ordered_amount[0][0] / total if ordered_amount and total else None,
                            "top3_concentration": sum(v for v, _ in ordered_amount[:3]) / total if total else None,
                            "hot_rank": ((hot_plate_ranks or {}).get(code) or {}).get("hot_rank", "—"),
                            "hot_strength": ((hot_plate_ranks or {}).get(code) or {}).get("strength"),
                            "hot_change_pct": ((hot_plate_ranks or {}).get(code) or {}).get("change_pct"),
                            "_members": members, "_ordered_amount": ordered_amount})
    low = min((row for row in price_rows if _num(row.get("pct_change")) is not None),
              key=lambda row: (_num(row.get("pct_change")), row["symbol"]), default=None)
    low_symbol = low["symbol"] if low else None
    selected: set[str] = set()
    selectable_plates = [p for p in plate_stats if p["class"] != "BROAD"]
    for scale in ("小", "中", "大"):
        group = [p for p in selectable_plates if p["scale"] == scale and p["strength_vs_market_pct"] is not None]
        strength = sorted(group, key=lambda p: (-p["strength_vs_market_pct"], -(p["up_ratio"] or 0), p["plate_id"]))
        if len(strength) >= 2:
            selected.update(p["plate_id"] for p in strength[:2])
        if strength:
            selected.add(min(strength, key=lambda p: (p["strength_vs_market_pct"], p["up_ratio"] or 0, p["plate_id"]))["plate_id"])
    selected.update(p["plate_id"] for p in sorted((p for p in selectable_plates if p["amount_yuan"] is not None),
                                                    key=lambda p: (-p["amount_yuan"], p["plate_id"]))[:3])
    amount_top3 = {p["plate_id"] for p in sorted((p for p in selectable_plates if p["amount_yuan"] is not None),
                                                  key=lambda p: (-p["amount_yuan"], p["plate_id"]))[:3]}
    for scale in ("小", "中", "大"):
        group = [p for p in selectable_plates if p["scale"] == scale and p["strength_vs_market_pct"] is not None]
        strength = sorted(group, key=lambda p: (-p["strength_vs_market_pct"], -(p["up_ratio"] or 0), p["plate_id"]))
        weak = min(group, key=lambda p: (p["strength_vs_market_pct"], p["up_ratio"] or 0, p["plate_id"])) if group else None
        for index, plate in enumerate(strength):
            reasons = []
            if index < 2: reasons.append("强")
            if weak is plate: reasons.append("弱")
            if plate["plate_id"] in amount_top3: reasons.append("额Top3")
            plate["selection_reasons"] = reasons
    yesterday_symbols = set(pool_by_symbol)
    plates_out = []
    for plate in sorted((p for p in selectable_plates if p["plate_id"] in selected),
                        key=lambda p: (p["scale"] or "", p["plate_id"])):
        members = plate["_members"]
        picks: set[str] = {row["symbol"] for _, row in plate["_ordered_amount"][:3]}
        price_members = [row for row in members if row.get("valid_price") and _num(row.get("pct_change")) is not None]
        if price_members:
            picks.add(min(price_members, key=lambda row: (-_num(row["pct_change"]), row["symbol"]))["symbol"])
            picks.add(min(price_members, key=lambda row: (_num(row["pct_change"]), row["symbol"]))["symbol"])
        picks.update(row["symbol"] for row in members if row["symbol"] in yesterday_symbols)
        if low_symbol and any(row["symbol"] == low_symbol for row in members):
            picks.add(low_symbol)
        seal_members = sorted((row for row in members if row["symbol"] in seals_verified),
                              key=lambda row: (-_num(row["seal"]["seal_amount_yuan"]), row["symbol"]))
        picks.update(row["symbol"] for row in seal_members[:2])
        stock_out = []
        for symbol in sorted(picks):
            row = by_symbol.get(symbol)
            if row is None:
                continue
            seal = row.get("seal", {})
            stock = {"symbol": symbol, "name": stock_name(symbol, row), "pct_change": _num(row.get("pct_change")),
                     "auction_amount_yuan": _num(row.get("auction_amount_yuan")),
                     "amount_rank": amount_ranks.get(symbol), "amount_top50": bool(row.get("amount_top50")),
                     "yesterday_streak": pool_by_symbol.get(symbol, {}).get("lb_days"),
                     "seal_amount_yuan": _num(seal.get("seal_amount_yuan")) if seal.get("status") in ("VERIFIED", "VERIFIED_REPLAY") else None,
                     "seal_rank": seal_ranks.get(symbol) if seal.get("status") in ("VERIFIED", "VERIFIED_REPLAY") else "UNKNOWN",
                     "seal_status": seal.get("status", "UNKNOWN"), "extremes": []}
            if stock["amount_top50"]:
                stock["extremes"].append("额Top50")
            if symbol == low_symbol:
                stock["extremes"].append("最大低开")
            if stock["seal_rank"] != "UNKNOWN" and stock["seal_rank"] is not None and int(stock["seal_rank"]) <= 10:
                stock["extremes"].append("封单Top10")
            stock["extremes"] = stock["extremes"][:3]
            stock_out.append(stock)
        if plate["top1_share"] is not None and plate["_ordered_amount"]:
            top_symbol = plate["_ordered_amount"][0][1]["symbol"]
            for stock in stock_out:
                if stock["symbol"] == top_symbol:
                    stock["extremes"].append("Top1金额占比")
                    stock["top1_amount_share"] = plate["top1_share"]
        clean = {key: value for key, value in plate.items() if not key.startswith("_")}
        clean["stocks"] = stock_out
        plates_out.append(clean)

    yesterday_reasons = yesterday_reasons or {}
    theme_assignments = theme_assignments or {}
    layers: dict[int, dict[str, list[dict[str, Any]]]] = {}
    for symbol, pool_row in pool_by_symbol.items():
        try:
            level = int(pool_row.get("lb_days", pool_row.get("streak", 1)))
        except (ValueError, TypeError):
            level = 1
        assignment = theme_assignments.get(symbol, {})
        primary = assignment.get("primary_theme") if isinstance(assignment, Mapping) else None
        theme_name = primary.get("name") if isinstance(primary, Mapping) else None
        source = assignment.get("source") if isinstance(assignment, Mapping) else None
        reasons = yesterday_reasons.get(symbol, yesterday_reasons.get(symbol.lstrip("0"), []))
        reason_input = {"reasons": reasons,
                        "pool_label": theme_name if source == "POOL_LABEL" else None,
                        "static": theme_name if source == "STATIC" else None}
        if not reasons and source == "REASON_LATEST" and theme_name:
            reason_input["reasons"] = [theme_name]
        reason = _primary_reason(pool_row, reason_input)
        row = by_symbol.get(symbol, {})
        seal = row.get("seal", {})
        layer = layers.setdefault(level, {})
        pct_change = _num(row.get("pct_change"))
        auction_price = _num(row.get("price", row.get("price_yuan")))
        valid_auction_price = row.get("valid_price") is True and auction_price is not None and auction_price > 0
        promotion_status = ("PROVISIONAL" if valid_auction_price and pct_change is not None and pct_change >= 9.5
                            else "UNVERIFIED")
        output_row = {"symbol": symbol, "name": stock_name(symbol, row, pool_row),
            "auction_amount_yuan": _num(row.get("auction_amount_yuan")), "pct_change": _num(row.get("pct_change")),
            "amount_rank": amount_ranks.get(symbol), "amount_top50": bool(row.get("amount_top50")),
            "seal_amount_yuan": _num(seal.get("seal_amount_yuan")) if seal.get("status") in ("VERIFIED", "VERIFIED_REPLAY") else None,
            "seal_rank": seal_ranks.get(symbol) if seal.get("status") in ("VERIFIED", "VERIFIED_REPLAY") else "UNKNOWN",
            "primary_reason": reason, "promotion_status": promotion_status, "extremes": []}
        if output_row["amount_top50"]:
            output_row["extremes"].append("额Top50")
        if output_row["seal_rank"] not in (None, "UNKNOWN") and int(output_row["seal_rank"]) <= 10:
            output_row["extremes"].append("封单Top10")
        if symbol == low_symbol:
            output_row["extremes"].append("最大低开")
        layer.setdefault(reason, []).append(output_row)
    ladder = []
    highest_level = max(layers, default=0)
    for level in sorted(layers, reverse=True):
        buckets = []
        n_layer = sum(len(rows) for rows in layers[level].values())
        level_rows = [row for grouped in layers[level].values() for row in grouped
                      if row["pct_change"] is not None]
        if level_rows:
            lo = min(row["pct_change"] for row in level_rows)
            hi = max(row["pct_change"] for row in level_rows)
            for reason, reason_rows in layers[level].items():
                for row in reason_rows:
                    if len(reason_rows) > 1 and row["pct_change"] in (lo, hi):
                        row["extremes"].append("同身位最大分歧")
        layer_amount = sum(_num(by_symbol.get(row["symbol"], {}).get("auction_amount_yuan")) or 0
                           for rows in layers[level].values() for row in rows)
        reason_groups = {reason: list(rows) for reason, rows in layers[level].items()}
        if level < highest_level:
            singletons = [row for reason, rows in reason_groups.items() if len(rows) == 1
                          for row in rows]
            if singletons:
                reason_groups = {reason: rows for reason, rows in reason_groups.items() if len(rows) != 1}
                reason_groups.setdefault("其他/单只", []).extend(singletons)
        for reason, rows in sorted(reason_groups.items()):
            rows.sort(key=lambda row: (row["seal_amount_yuan"] is None,
                                       -(row["seal_amount_yuan"] or 0),
                                       -(row["auction_amount_yuan"] or 0), row["symbol"]))
            vals = [row["pct_change"] for row in rows if row["pct_change"] is not None]
            n_open = sum(value > 0 for value in vals)
            buckets.append({"reason": reason, "count": len(rows),
                "auction_amount_yuan": sum(row["auction_amount_yuan"] or 0 for row in rows) if any(row["auction_amount_yuan"] is not None for row in rows) else None,
                "layer_amount_share": (sum(row["auction_amount_yuan"] or 0 for row in rows) / layer_amount) if layer_amount else None,
                "high_open_ratio": n_open / len(vals) if vals else None,
                "low_open_ratio": sum(value < 0 for value in vals) / len(vals) if vals else None,
                "valid_price_count": len(vals), "median_pct": _median(vals),
                "provisional_promotion_count": sum(row["promotion_status"] == "PROVISIONAL" for row in rows),
                "unverified_promotion_count": sum(row["promotion_status"] != "PROVISIONAL" for row in rows),
                "verified_seal_amount_yuan": sum(row["seal_amount_yuan"] or 0 for row in rows if row["seal_amount_yuan"] is not None) if any(row["seal_amount_yuan"] is not None for row in rows) else None,
                "stocks": rows})
        ladder.append({"yesterday_streak": level, "count": n_layer, "buckets": buckets})

    low = min((row for row in price_rows if _num(row.get("pct_change")) is not None),
              key=lambda row: (_num(row.get("pct_change")), row["symbol"]), default=None)
    low_symbol = low["symbol"] if low else None
    overview = {"auction_amount_yuan": market_amount, "auction_amount_yi": market_amount / 1e8 if market_amount is not None else None,
        "amount_multiple_64d": {"value": multiple, "percentile": percentile,
            "sample_n": len(history_values), "window_n": 64,
            "basis": amount_baseline_basis,
            "status": "READY" if len(history_values) == 64 and baseline_status == "READY" else "PARTIAL"},
        "valid_amount_n": len(valid_amounts), "universe_n": len(by_symbol),
        "valid_price_n": len(changes), "up_ratio": {"numerator": up_n, "denominator": len(changes), "ratio": market_up_ratio},
        "market_median_pct": market_median, "yesterday_limit_high_open": pool_open,
        "yesterday_vs_market_pp": high_open_pp,
        "verified_seal_sum_yuan": verified_sum if seals_verified else None,
        "verified_seal_scope": f"{len(seals_verified)}/{candidates}" if seals_verified else "待核实",
        "verified_seal_label": stock_facts.get("seal_label", "待核实"),
        "market_structure": (f"有效价格上涨{up_n}/{len(changes)}；中位涨幅{market_median:.2f}%" if changes and market_median is not None else "有效价格样本不足")}
    # Rule-based extreme values are attached to relevant facts, plus a compact footer.
    gap = {"symbol": low["symbol"], "pct_change": _num(low.get("pct_change"))} if low else None
    extremes = {"largest_gap_down": gap,
        "max_same_streak_dispersion": [{"yesterday_streak": level,
            "range_pct": max((row["pct_change"] for rows in groups.values() for row in rows if row["pct_change"] is not None), default=None)
                - min((row["pct_change"] for rows in groups.values() for row in rows if row["pct_change"] is not None), default=None)}
            for level, groups in sorted(layers.items(), reverse=True)
            if any(row["pct_change"] is not None for rows in groups.values() for row in rows)],
        "footer": "；".join(filter(None, [f"最大低开 {gap['symbol']} {gap['pct_change']:.2f}%" if gap else None,
            f"竞价金额Top1占比 {max((p['top1_share'] for p in plate_stats if p['top1_share'] is not None), default=0):.1%}"]))}
    return {"schema": SCHEMA, "trade_date": trade_date, "asof": asof, "status": "PARTIAL",
            "overview": overview, "plates": plates_out, "yesterday_ladder": ladder, "extremes": extremes,
            "compatibility_aliases": {"equal_weight_pct": {"alias_for": "member_median_pct",
                                                               "label": "兼容别名"}},
            "lineage": {"auction_stock_facts": stock_facts.get("version"), "hot_plate_ranks": "UNKNOWN" if not hot_plate_ranks else "PROVIDED"}}


def store_auction_panorama(product: Mapping[str, Any], *, artifact_root: str | Path | None = None,
                           redis: Any = None, ttl: int = 3600) -> dict[str, Any]:
    """Append immutable cold artifact and publish a bounded Core-only hot view."""
    root = Path(artifact_root or os.getenv("CORE_DATA_ROOT", str(Path.home() / "core-data")))
    catalog = ArtifactCatalog(root)
    rows = [{"row_kind": "product", "trade_date": product["trade_date"], "product_json": json.dumps(product, ensure_ascii=False, separators=(",", ":"))}]
    available_at = datetime.now(TZ).isoformat()
    manifest = catalog.commit("auction_panorama", str(product["trade_date"]), rows,
        {"schema": SCHEMA, "trade_date": product["trade_date"], "as_of": product["asof"],
         "available_at": available_at, "business_status": product.get("status", "PARTIAL"),
         "quality": {"plate_count": len(product.get("plates", [])),
                     "yesterday_stock_count": sum(bucket["count"] for layer in product.get("yesterday_ladder", [])
                                                   for bucket in layer.get("buckets", []))}},
        source="auction_panorama")
    if redis is not None:
        hot = {"schema": SCHEMA, "version": manifest["version"], "trade_date": product["trade_date"],
               "asof": product["asof"], "available_at": available_at, "overview": product["overview"],
               "plate_ids": [row["plate_id"] for row in product["plates"]],
               "yesterday_symbols": [stock["symbol"] for layer in product["yesterday_ladder"]
                   for bucket in layer["buckets"] for stock in bucket["stocks"]]}
        redis.set(f"core:cache:data:auction_panorama:{product['trade_date']}",
                  json.dumps(hot, ensure_ascii=False, separators=(",", ":")), ex=ttl)
    return manifest
