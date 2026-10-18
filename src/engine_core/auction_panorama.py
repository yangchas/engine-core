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


def _has_valid_auction_price(row: Mapping[str, Any]) -> bool:
    price = _num(row.get("price", row.get("price_yuan")))
    return row.get("valid_price") is True and price is not None and price > 0


def _at_official_limit(row: Mapping[str, Any]) -> bool | None:
    price = _num(row.get("price", row.get("price_yuan")))
    limit = _num(row.get("official_limit_up_price"))
    if not _has_valid_auction_price(row) or limit is None or limit <= 0:
        return None
    return round(price * 100) == round(limit * 100)


def _median(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _reason_label(value: Any) -> str:
    """Keep the concrete reason headline while dropping its long evidence text."""
    text = str(value or "").split(";", 1)[0].split("；", 1)[0].splitlines()[0].strip()
    return text[:40] or "原因未核实"


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


def _primary_reason(pool_row: Mapping[str, Any], historical: Any,
                    expected_event_date: str | None = None) -> str:
    """Priority: explicit yesterday-pool label, then first D-1 history tag, then static tag."""
    for key in ("pool_reason", "primary_reason", "reason", "limit_reason"):
        value = pool_row.get(key)
        if value not in (None, ""):
            return _reason_label(value)
    assignment = historical if isinstance(historical, Mapping) else {}
    historical = assignment.get("reasons", assignment.get("tags", historical))
    if isinstance(historical, str):
        historical = [historical]
    if isinstance(historical, Sequence) and not isinstance(historical, (str, bytes)):
        if expected_event_date:
            expected = str(expected_event_date)[:10]
            dated = [item for item in historical if isinstance(item, Mapping)
                     and str(item.get("event_date", item.get("date", "")))[:10] == expected]
        elif any(isinstance(item, Mapping) for item in historical):
            dated = []
        else:
            dated = list(historical)
        historical = [item.get("reason", item.get("Reason", item.get("label")))
                      if isinstance(item, Mapping) else item for item in dated]
    if historical:
        return sorted({_reason_label(value) for value in historical if value not in (None, "")})[0]
    if assignment.get("static") not in (None, ""):
        return str(assignment["static"]).strip()
    for key in ("static_reason", "static_tag", "theme"):
        if pool_row.get(key) not in (None, ""):
            return str(pool_row[key]).strip()
    return "原因未核实"


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
                           amount_baseline_basis: str = "PIT",
                           previous_trade_date: str | None = None) -> dict[str, Any]:
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
    price_rows = [row for row in by_symbol.values() if _has_valid_auction_price(row)
                  and _num(row.get("pct_change")) is not None]
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
    pool_price_rows = [by_symbol[s] for s in pool_by_symbol if s in by_symbol and _has_valid_auction_price(by_symbol[s])
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
        valid = [row for row in members if _has_valid_auction_price(row)
                 and _num(row.get("pct_change")) is not None]
        pcts = [_num(row.get("pct_change")) for row in valid]
        pcts = [v for v in pcts if v is not None]
        size = len(plate["symbols"])
        scale = "小" if 5 <= size <= 19 else "中" if 20 <= size <= 99 else "大" if size >= 100 else None
        member_amount = [_num(row.get("auction_amount_yuan")) for row in members]
        valid_member_amount = [v for v in member_amount if v is not None and v >= 0]
        total = sum(valid_member_amount) if valid_member_amount else None
        ordered_amount = sorted((( _num(row.get("auction_amount_yuan")), row) for row in members
                                 if _num(row.get("auction_amount_yuan")) is not None), key=lambda x: (-x[0], x[1]["symbol"]))
        up = sum(value > 0 for value in pcts)
        plate_stats.append({"plate_id": code, "plate_name": plate["plate_name"], "count": size,
                            "class": plate_classes.get(code, "UNKNOWN"),
                            "scale": scale, "amount_yuan": total,
                            "market_share": total / market_amount if total is not None and market_amount else None,
                            "member_median_pct": _median(pcts), "equal_weight_pct": _median(pcts),
                            "up_count": up, "valid_price_count": len(pcts),
                            "valid_amount_count": len(valid_member_amount),
                            "up_label": f"上涨 {up}/{len(pcts)}",
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
    selection_sources: dict[str, set[str]] = {}
    def select_plate(code: str, *sources: str) -> None:
        selected.add(code)
        selection_sources.setdefault(code, set()).update(sources)
    selectable_plates = [p for p in plate_stats if p["class"] != "BROAD"]
    for scale in ("小", "中", "大"):
        group = [p for p in selectable_plates if p["scale"] == scale and p["strength_vs_market_pct"] is not None]
        strength = sorted(group, key=lambda p: (-p["strength_vs_market_pct"], -(p["up_ratio"] or 0), p["plate_id"]))
        if len(strength) >= 2:
            for plate in strength[:2]:
                select_plate(plate["plate_id"], "强")
        if strength:
            weak = min(strength, key=lambda p: (p["strength_vs_market_pct"], p["up_ratio"] or 0, p["plate_id"]))
            select_plate(weak["plate_id"], "弱")
    amount_top3 = {p["plate_id"] for p in sorted((p for p in selectable_plates if p["amount_yuan"] is not None),
                                                  key=lambda p: (-p["amount_yuan"], p["plate_id"]))[:3]}
    for code in amount_top3:
        select_plate(code, "额Top3")
    for scale in ("小", "中", "大"):
        group = [p for p in selectable_plates if p["scale"] == scale and p["strength_vs_market_pct"] is not None]
        strength = sorted(group, key=lambda p: (-p["strength_vs_market_pct"], -(p["up_ratio"] or 0), p["plate_id"]))
        weak = min(group, key=lambda p: (p["strength_vs_market_pct"], p["up_ratio"] or 0, p["plate_id"])) if group else None
    # Forced high-board and seal themes may be broad. BROAD only excludes a
    # plate from the size/strength and amount shortlist, not from explicit
    # evidence-driven inclusion.
    plate_by_name: dict[str, list[str]] = {}
    for plate in plate_stats:
        plate_by_name.setdefault(str(plate["plate_name"]).strip(), []).append(plate["plate_id"])
    plate_by_id = {plate["plate_id"]: plate for plate in plate_stats}
    theme_assignments = theme_assignments or {}
    def assigned_plate_codes(symbol: str) -> list[str]:
        assignment = theme_assignments.get(symbol, {})
        if not isinstance(assignment, Mapping):
            return []
        candidate = assignment.get("primary_theme")
        if not isinstance(candidate, Mapping):
            return []
        code = str(candidate.get("id", candidate.get("code", "")) or "")
        name = str(candidate.get("name", "") or "").strip()
        codes = [code] if code in plate_by_id else plate_by_name.get(name, [])
        return list(dict.fromkeys(codes))
    def streak_for(symbol: str) -> float | None:
        pool = pool_by_symbol.get(symbol, {})
        return _num(pool.get("lb_days", pool.get("streak", 1)))
    for symbol, pool_row in pool_by_symbol.items():
        if (streak_for(symbol) or 0) < 2:
            continue
        reason = _primary_reason(pool_row, {"reasons": (yesterday_reasons or {}).get(
            symbol, (yesterday_reasons or {}).get(symbol.lstrip("0"), []))}, previous_trade_date)
        exact_codes = plate_by_name.get(reason, [])
        if exact_codes:
            for code in exact_codes:
                select_plate(code, "高标")
        else:
            for code in assigned_plate_codes(symbol):
                select_plate(code, "高标", "关联")
    seal_top10 = {symbol for symbol, seal in seals.items()
                  if seal.get("status") in ("VERIFIED", "VERIFIED_REPLAY")
                  and (_num(seal.get("seal_rank")) or 999999) <= 10}
    for symbol in seal_top10:
        for code in assigned_plate_codes(symbol):
            select_plate(code, "封单")
    for plate in plate_stats:
        plate["selection_reasons"] = [reason for reason in
            ("强", "弱", "额Top3", "高标", "封单", "关联")
            if reason in selection_sources.get(plate["plate_id"], set())]
    global_amount_top3 = {symbol for symbol, rank in amount_ranks.items()
                          if _num(rank) is not None and _num(rank) <= 3}
    plates_out = []
    for plate in sorted((p for p in plate_stats if p["plate_id"] in selected),
                        key=lambda p: (p["scale"] or "", p["plate_id"])):
        members = plate["_members"]
        member_symbols = {row["symbol"] for row in members}
        picks: set[str] = {row["symbol"] for row in members
                           if row["symbol"] in seal_top10 or
                           ((streak_for(row["symbol"]) or 0) >= 2)}
        # A PIT theme assignment can name a board while that day's point-in-time
        # membership snapshot omits the stock. Keep verified seal evidence
        # visible under its assigned board, but label it as an association and
        # do not include it in member counts or price/amount denominators.
        associated_seals = {symbol for symbol in seal_top10
                            if plate["plate_id"] in assigned_plate_codes(symbol)
                            and symbol not in member_symbols}
        picks.update(associated_seals)
        picks.update(row["symbol"] for row in members if row["symbol"] in global_amount_top3)
        price_members = [row for row in members if _has_valid_auction_price(row)
                         and _num(row.get("pct_change")) is not None]
        if price_members:
            picks.add(min(price_members, key=lambda row: (-_num(row["pct_change"]), row["symbol"]))["symbol"])
            picks.add(min(price_members, key=lambda row: (_num(row["pct_change"]), row["symbol"]))["symbol"])
        if low_symbol and any(row["symbol"] == low_symbol for row in members):
            picks.add(low_symbol)
        plate_price_rows = [row for row in members if _has_valid_auction_price(row)
                            and _num(row.get("pct_change")) is not None]
        if plate_price_rows:
            picks.add(max(plate_price_rows, key=lambda row: (_num(row["pct_change"]), row["symbol"]))["symbol"])
            picks.add(min(plate_price_rows, key=lambda row: (_num(row["pct_change"]), row["symbol"]))["symbol"])
        def member_priority(symbol: str) -> tuple[int, int, int, str]:
            high_or_sealed = (streak_for(symbol) or 0) >= 2 or symbol in seal_top10
            amount_rank = _num(amount_ranks.get(symbol))
            is_amount_top3 = amount_rank is not None and amount_rank <= 3
            row = by_symbol.get(symbol, {})
            is_extreme = bool(row.get("pct_change") is not None and
                              (row.get("pct_change") == min((_num(item.get("pct_change")) for item in plate_price_rows), default=None)
                               or row.get("pct_change") == max((_num(item.get("pct_change")) for item in plate_price_rows), default=None)))
            return (0 if high_or_sealed else 1 if is_amount_top3 else 2 if is_extreme else 3,
                    0 if (streak_for(symbol) or 0) >= 2 else 1,
                    int(amount_rank) if amount_rank is not None else 999999, symbol)
        stock_out = []
        for symbol in sorted(picks, key=member_priority):
            row = by_symbol.get(symbol)
            if row is None:
                continue
            seal = row.get("seal", {})
            stock = {"symbol": symbol, "name": stock_name(symbol, row), "pct_change": _num(row.get("pct_change")),
                     "auction_amount_yuan": _num(row.get("auction_amount_yuan")),
                     "amount_rank": amount_ranks.get(symbol), "amount_top50": bool(row.get("amount_top50")),
                     "membership_status": "MEMBER" if symbol in member_symbols else "THEME_ASSOCIATION_ONLY",
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
        reason_input = {"reasons": reasons}
        reason = _primary_reason(pool_row, reason_input, previous_trade_date)
        row = by_symbol.get(symbol, {})
        seal = row.get("seal", {})
        layer = layers.setdefault(level, {})
        pct_change = _num(row.get("pct_change"))
        valid_auction_price = _has_valid_auction_price(row)
        seal_verified = seal.get("status") in ("VERIFIED", "VERIFIED_REPLAY")
        at_limit = _at_official_limit(row)
        if valid_auction_price and (seal_verified or at_limit is True):
            promotion_status = "PROVISIONAL"
        elif not valid_auction_price or at_limit is None or pct_change is None:
            promotion_status = "UNVERIFIED"
        else:
            promotion_status = "NOT_PROMOTED"
        pool_cause = next((pool_row.get(key) for key in ("pool_reason", "primary_reason", "reason", "limit_reason")
                           if pool_row.get(key) not in (None, "")), None)
        reason_source_date = None
        if pool_cause is not None and _reason_label(pool_cause) == reason:
            reason_source_date = str(pool_row.get("event_date", pool_row.get("date", "")))[:10] or None
        elif previous_trade_date:
            for reason_row in reasons if isinstance(reasons, Sequence) and not isinstance(reasons, (str, bytes)) else ():
                if (isinstance(reason_row, Mapping)
                        and str(reason_row.get("event_date", reason_row.get("date", "")))[:10] == previous_trade_date
                        and _reason_label(reason_row.get("reason", reason_row.get("Reason", ""))) == reason):
                    reason_source_date = previous_trade_date
                    break
        output_row = {"symbol": symbol, "name": stock_name(symbol, row, pool_row),
            "auction_amount_yuan": _num(row.get("auction_amount_yuan")), "pct_change": _num(row.get("pct_change")),
            "amount_rank": amount_ranks.get(symbol), "amount_top50": bool(row.get("amount_top50")),
            "seal_amount_yuan": _num(seal.get("seal_amount_yuan")) if seal.get("status") in ("VERIFIED", "VERIFIED_REPLAY") else None,
            "seal_rank": seal_ranks.get(symbol) if seal.get("status") in ("VERIFIED", "VERIFIED_REPLAY") else "UNKNOWN",
            "primary_reason": reason,
            "primary_reason_source_date": reason_source_date,
            "related_theme": theme_name if theme_name and reason == "原因未核实" else None,
            "promotion_status": promotion_status,
            "at_official_limit": at_limit is True, "extremes": []}
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
                reason_groups.setdefault("单只原因合集", []).extend(singletons)
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
                "unverified_promotion_count": sum(row["promotion_status"] == "UNVERIFIED" for row in rows),
                "not_promoted_count": sum(row["promotion_status"] == "NOT_PROMOTED" for row in rows),
                "verified_seal_amount_yuan": sum(row["seal_amount_yuan"] or 0 for row in rows if row["seal_amount_yuan"] is not None) if any(row["seal_amount_yuan"] is not None for row in rows) else None,
                "stocks": rows})
        ladder.append({"yesterday_streak": level, "count": n_layer, "buckets": buckets})

    low = min((row for row in price_rows if _num(row.get("pct_change")) is not None),
              key=lambda row: (_num(row.get("pct_change")), row["symbol"]), default=None)
    low_symbol = low["symbol"] if low else None
    yesterday_rows = [row for symbol, row in by_symbol.items() if symbol in pool_by_symbol]
    yesterday_amounts = [_num(row.get("auction_amount_yuan")) for row in yesterday_rows]
    yesterday_valid_amounts = [value for value in yesterday_amounts if value is not None and value >= 0]
    yesterday_total = (sum(yesterday_valid_amounts)
                       if len(yesterday_valid_amounts) == len(yesterday_rows) and yesterday_rows else None)
    yesterday_price_rows = [row for row in yesterday_rows if _has_valid_auction_price(row)
                            and _num(row.get("pct_change")) is not None]
    yesterday_pcts = [_num(row.get("pct_change")) for row in yesterday_price_rows]
    yesterday_pcts = [value for value in yesterday_pcts if value is not None]
    yesterday_open_counts = {"high": sum(value > 0 for value in yesterday_pcts),
                             "flat": sum(value == 0 for value in yesterday_pcts),
                             "low": sum(value < 0 for value in yesterday_pcts)}
    def _streak(row: Mapping[str, Any]) -> float | None:
        pool_row = pool_by_symbol[row["symbol"]]
        return _num(pool_row.get("lb_days", pool_row.get("streak", 1)))
    multi_amounts = [_num(row.get("auction_amount_yuan")) for row in yesterday_rows
                     if (_streak(row) or 0) >= 2]
    first_amounts = [_num(row.get("auction_amount_yuan")) for row in yesterday_rows
                     if _streak(row) == 1]
    multi_amounts = [value for value in multi_amounts if value is not None and value >= 0]
    first_amounts = [value for value in first_amounts if value is not None and value >= 0]
    multi_mean = statistics.mean(multi_amounts) if multi_amounts else None
    first_mean = statistics.mean(first_amounts) if first_amounts else None
    yesterday_worst = sorted(yesterday_price_rows,
                             key=lambda row: (_num(row.get("pct_change")), row["symbol"]))[:2]
    pool_seals = [row for row in yesterday_rows if row.get("seal", {}).get("status") in ("VERIFIED", "VERIFIED_REPLAY")
                  and _num(row.get("seal", {}).get("seal_amount_yuan")) is not None]
    overview = {"auction_amount_yuan": market_amount, "auction_amount_yi": market_amount / 1e8 if market_amount is not None else None,
        "yesterday_pool_auction_amount_yuan": yesterday_total,
        "yesterday_pool_market_share": (yesterday_total / market_amount
                                        if yesterday_total is not None and market_amount else None),
        "yesterday_pool_open_counts": yesterday_open_counts,
        "yesterday_pool_open_valid_n": len(yesterday_pcts),
        "multi_board_mean_auction_amount_yuan": multi_mean,
        "first_board_mean_auction_amount_yuan": first_mean,
        "multi_to_first_board_mean_multiple": multi_mean / first_mean if multi_mean is not None and first_mean else None,
        "yesterday_pool_worst_two": [{"symbol": row["symbol"], "name": stock_name(row["symbol"], row, pool_by_symbol[row["symbol"]]),
                                       "pct_change": _num(row.get("pct_change"))} for row in yesterday_worst],
        "yesterday_pool_verified_seal_n": len(pool_seals),
        "yesterday_pool_verified_seal_sum_yuan": (sum(_num(row["seal"]["seal_amount_yuan"]) for row in pool_seals)
                                                   if pool_seals else None),
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
