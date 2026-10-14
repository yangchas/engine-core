"""Build date-scoped offline plate membership and auction facts from TD read-only data."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from dataclasses import replace
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from engine_core.limit_up_pool_derivation import derive_limit_up_pool_from_daily_klines
from engine_core.plate_aggregation import StockFact, aggregate_plates
from engine_core.plate_kind import classify_plate_kind
from engine_core.plate_membership import (
    MEMBERSHIP_POLICY_LEGACY_V1,
    MEMBERSHIP_POLICY_USER_PRIORITY_V1,
    PlateHierarchy,
    apply_hierarchy,
    build_memberships,
    default_alias_hierarchy,
)
from engine_core.plate_hierarchy_kaipan import (
    PoolRow, build_hierarchy_as_of, parse_kaipan_pool, parse_kaipan_reason_hierarchy,
    parse_kaipan_reason_items,
)
from engine_core.plate_rules import build_plate_candidates_from_reason
from engine_core.plate_rules import normalize_plate_name
from engine_core.stock_plate_assignment import build_stock_plate_assignments

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def ensure_empty_output_dir(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError(f"output directory must be empty: {path}")
    path.mkdir(parents=True, exist_ok=True)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _reason_candidates(rows: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for symbol, items in rows.items():
        candidates: list[str] = []
        for row in items:
            candidates.extend(build_plate_candidates_from_reason(
                reason=str(row.get("reason") or ""), group_str=str(row.get("group_str") or ""),
                gnsm=str(row.get("gnsm") or ""),
            ))
        if candidates:
            result[str(symbol)] = list(dict.fromkeys(candidates))
    return result


def _kaipan_reasons_for_day(
    rows: Mapping[str, Sequence[Mapping[str, Any]]], trade_date: str, pool_symbols: set[str],
    conclusion_phase: str = "AUCTION",
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Select dated reasons for every Kaipan file available by the requested date."""
    del pool_symbols  # Kaipan reason coverage is independent of yesterday's limit-up pool.
    if conclusion_phase not in {"AUCTION", "EOD"}:
        raise ValueError("conclusion_phase must be AUCTION or EOD")
    dated_mode = bool(rows)
    selected: dict[str, list[dict[str, Any]]] = {}
    used = excluded_future = excluded_no_date = excluded_same_day = 0
    if dated_mode:
        cutoff = date.fromisoformat(trade_date)
        for symbol in rows:
            eligible: list[tuple[date, dict[str, Any]]] = []
            for raw in rows.get(symbol, ()):
                raw_date = raw.get("Date")
                try:
                    if not isinstance(raw_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
                        raise ValueError("Date must use YYYY-MM-DD")
                    row_date = date.fromisoformat(str(raw_date))
                except (TypeError, ValueError):
                    excluded_no_date += 1
                    continue
                if row_date > cutoff:
                    excluded_future += 1
                    continue
                if row_date == cutoff and conclusion_phase == "AUCTION":
                    excluded_same_day += 1
                    continue
                eligible.append((row_date, dict(raw)))
            eligible.sort(key=lambda item: (str(item[1].get("Type", "1")) != "0", -item[0].toordinal()))
            if eligible:
                selected[str(symbol)] = [{
                    **row,
                    "reason": row.get("Reason", row.get("reason", "")),
                    "group_str": row.get("Group_Str", row.get("group_str", "")),
                    "gnsm": row.get("GNSM", row.get("gnsm", "")),
                } for _, row in eligible]
                used += len(eligible)
    return selected, {
        "used": used, "excluded_future": excluded_future, "excluded_no_date": excluded_no_date,
        "excluded_same_day": excluded_same_day,
        "symbol_count_used": len(selected), "asof_enabled": dated_mode,
        "type0": sum(str(row.get("Type", "")) == "0" for items in selected.values() for row in items),
        "type1": sum(str(row.get("Type", "")) == "1" for items in selected.values() for row in items),
    }


def _reason_pool_for_day(
    rows: Mapping[str, Sequence[Mapping[str, Any]]], prior_day: str | None,
    td_pool_rows: Sequence[Mapping[str, Any]],
) -> dict[str, PoolRow]:
    if prior_day is None:
        return {}
    td_by_symbol = {str(row["symbol"]): row for row in td_pool_rows}
    output: dict[str, PoolRow] = {}
    for symbol, items in rows.items():
        matching = [row for row in items if row.get("Date", row.get("date")) == prior_day
                    and str(row.get("Type", row.get("type", ""))) == "0"]
        if not matching:
            continue
        row = matching[0]
        candidates = build_plate_candidates_from_reason(reason=str(row.get("Reason", row.get("reason", ""))))
        td_row = td_by_symbol.get(str(symbol), {})
        try:
            lb_days = int(td_row["lb_days"]) if td_row.get("lb_days") is not None else None
        except (TypeError, ValueError, OverflowError):
            lb_days = None
        output[str(symbol)] = PoolRow(candidates[0] if candidates else "", lb_days)
    return output


def _tag_reason_memberships(memberships: Any, reasons: Mapping[str, Sequence[Mapping[str, Any]]]) -> Any:
    by_symbol: dict[str, tuple[Any, ...]] = {}
    for symbol, rows in memberships.by_symbol.items():
        candidates_by_type: dict[str, set[str]] = {"0": set(), "1": set()}
        for reason in reasons.get(symbol, ()):
            reason_type = "0" if str(reason.get("Type", "")) == "0" else "1"
            candidates_by_type[reason_type].update(_reason_candidates({symbol: [reason]}).get(symbol, ()))
        tagged = []
        for row in rows:
            sources = row.source.split(",")
            if "reason" in sources:
                if row.plate in candidates_by_type["0"]:
                    label = "reason_limit_up"
                elif row.plate in candidates_by_type["1"]:
                    label = "reason_move"
                else:
                    tagged.append(row)
                    continue
                tagged.append(replace(row, source=",".join(label if source == "reason" else source
                                                           for source in sources)))
            else:
                tagged.append(row)
        by_symbol[symbol] = tuple(tagged)
    return replace(memberships, by_symbol=by_symbol)


def _kline_by_day(klines: Mapping[str, Sequence[Sequence[Any]]]) -> dict[str, dict[str, Sequence[Any]]]:
    output: dict[str, dict[str, Sequence[Any]]] = {}
    for symbol, rows in klines.items():
        for row in rows:
            if len(row) == 9 and isinstance(row[0], (str, date)):  # taos returns datetime for ts
                day = str(row[0].isoformat() if isinstance(row[0], date) else row[0])[:10]
                try:
                    date.fromisoformat(day)
                except ValueError:
                    continue
                output.setdefault(day, {})[str(symbol)] = row
    return output


def _jsonable_memberships(
    memberships: Any, hot_plate_metadata_by_name: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    hot_plate_metadata_by_name = hot_plate_metadata_by_name or {}
    return {
        "policy": memberships.policy,
        "rule_version": memberships.rule_version,
        "content_hash": memberships.content_hash,
        "by_symbol": {
            symbol: [
                {"plate": row.plate, "kind": classify_plate_kind(row.plate),
                 "tier": row.tier, "rank_in_stock": row.rank_in_stock,
                 "source": row.source, "generic": row.generic, "hard_generic": row.hard_generic,
                 "ancestors": list(row.ancestors), "flags": list(row.flags),
                 "hot_rank": hot_plate_metadata_by_name.get(row.plate, {}).get("hot_rank"),
                 "hot_code": hot_plate_metadata_by_name.get(row.plate, {}).get("hot_code")}
                for row in rows
            ] for symbol, rows in sorted(memberships.by_symbol.items())
        },
        "legacy_weights": {symbol: [[plate, weight] for plate, weight in rows]
                           for symbol, rows in sorted(memberships.legacy_weights.items())},
    }


def _jsonable_aggregation(aggregation: Any) -> dict[str, Any]:
    return {
        "content_hash": aggregation.content_hash,
        "rule_version": aggregation.rule_version,
        "market": aggregation.market.__dict__,
        "stats": [
            {**row.__dict__, "tier_counts": {str(k): v for k, v in row.tier_counts.items()},
             "flags": list(row.flags)} for row in aggregation.stats
        ],
    }


def build_day(
    *, trade_date: str, klines_by_symbol: Mapping[str, Sequence[Sequence[Any]]],
    auction_rows: Mapping[str, Mapping[str, Any]], snapshot: Mapping[str, Any],
    reasons_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    reason_asof_unknown: bool, auction_0924_rows: Mapping[str, Mapping[str, Any]] | None = None,
    min_auction_rows: int = 4000, skipped_kline_table_symbol_count: int = 0,
    kaipan_pool_rows: Mapping[str, PoolRow] | None = None,
    kaipan_pool_partial: bool = False,
    kaipan_hierarchy: PlateHierarchy | None = None,
    kaipan_reasons_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    kaipan_reason_items: Sequence[Any] | None = None,
    hot_plate_input: Mapping[str, Any] | None = None,
    snapshot_asof: str | None = None,
    conclusion_phase: str = "AUCTION",
) -> dict[str, Any]:
    """Pure daily composition; accepts loaded data and performs no I/O."""
    if not isinstance(snapshot.get("stock_plate"), Mapping) or not isinstance(snapshot.get("s2p"), Mapping):
        raise ValueError("snapshot must contain stock_plate and s2p mappings")
    auction_0924_rows = auction_0924_rows or {}
    daily_rows = _kline_by_day(klines_by_symbol)
    sessions = sorted(daily_rows)
    prior_sessions = [day for day in sessions if day < trade_date]
    prior_day = prior_sessions[-1] if prior_sessions else None
    hot_plate_input = hot_plate_input if prior_day and hot_plate_input else None
    hot_plate_rows = list(hot_plate_input.get("rows", ())) if hot_plate_input else []
    hot_plate_names = [row["name"] for row in hot_plate_rows]
    prior_close_rows = daily_rows.get(prior_day, {}) if prior_day else {}
    current_to_previous = {symbol: row for symbol, row in prior_close_rows.items()}

    td_pool_rows: tuple[dict[str, Any], ...] = ()
    td_pool_symbols: set[str] = set()
    derivation_hash = None
    if prior_day:
        derivation = derive_limit_up_pool_from_daily_klines(
            klines_by_symbol, prior_day, trading_dates=sessions,
        )
        td_pool_rows = derivation.rows
        td_pool_symbols = {str(row["symbol"]) for row in td_pool_rows}
        derivation_hash = derivation.content_hash

    reason_pool_rows = _reason_pool_for_day(kaipan_reasons_by_symbol or {}, prior_day, td_pool_rows)
    valid_kaipan_pool = kaipan_pool_rows is not None and not kaipan_pool_partial
    if valid_kaipan_pool:
        selected_pool_rows = kaipan_pool_rows
        pool_source = "kaipan_pool"
    elif reason_pool_rows:
        selected_pool_rows = reason_pool_rows
        pool_source = "kaipan_reasons"
    else:
        selected_pool_rows = None

    if selected_pool_rows is None and td_pool_symbols:
        pool_symbols = td_pool_symbols
        pool_plates = {str(row["symbol"]): row.get("plate") for row in td_pool_rows if row.get("plate")}
        pool_lb_days = {str(row["symbol"]): row.get("lb_days") for row in td_pool_rows}
        pool_source = "td_kline"
    elif selected_pool_rows is not None:
        pool_symbols = set(selected_pool_rows)
        pool_plates = {symbol: row.plate for symbol, row in selected_pool_rows.items() if row.plate}
        pool_lb_days = {symbol: row.lb_days for symbol, row in selected_pool_rows.items()}
    else:
        pool_symbols, pool_plates, pool_lb_days = set(), {}, {}
        pool_source = "missing"

    static_assignment = build_stock_plate_assignments(
        trade_date=trade_date,
        static_primary_by_symbol=snapshot["stock_plate"],
        static_themes_by_symbol=snapshot["s2p"],
    )
    static_themes: dict[str, list[str]] = {}
    static_primary: dict[str, str] = {}
    for symbol, assignment in static_assignment.assignments.items():
        if assignment.primary_plate:
            static_primary[symbol] = assignment.primary_plate
        static_themes[symbol] = list(dict.fromkeys(
            ([assignment.primary_plate] if assignment.primary_plate else []) + list(assignment.themes)
        ))

    if conclusion_phase not in {"AUCTION", "EOD"}:
        raise ValueError("conclusion_phase must be AUCTION or EOD")
    kaipan_reason_rows, reason_counts = _kaipan_reasons_for_day(
        kaipan_reasons_by_symbol or {}, trade_date, set(map(str, pool_symbols)), conclusion_phase,
    )
    hierarchy_counts = {"used": 0, "excluded_future": 0, "excluded_no_date": 0}
    if kaipan_reason_items is not None:
        kaipan_hierarchy = build_hierarchy_as_of(
            kaipan_reason_items, trade_date, inclusive=conclusion_phase == "EOD", counts=hierarchy_counts,
        )
    elif kaipan_reasons_by_symbol is not None:
        hierarchy_responses = [
            {"StockID": str(symbol), "List": list(rows)}
            for symbol, rows in kaipan_reasons_by_symbol.items()
        ]
        kaipan_hierarchy = parse_kaipan_reason_hierarchy(
            hierarchy_responses, as_of_date=trade_date, counts=hierarchy_counts,
            inclusive=conclusion_phase == "EOD",
        )
    if kaipan_reasons_by_symbol is None:
        legacy_reason_rows = {str(symbol): rows for symbol, rows in reasons_by_symbol.items()}
        kaipan_asof_candidates: dict[str, list[str]] = {}
        unavailable_symbols: set[str] = set()
    else:
        kaipan_symbols = set(map(str, kaipan_reasons_by_symbol))
        legacy_reason_rows = {
            str(symbol): rows for symbol, rows in reasons_by_symbol.items()
            if str(symbol) not in kaipan_symbols
        }
        kaipan_asof_candidates = _reason_candidates(kaipan_reason_rows)
        unavailable_symbols = kaipan_symbols - set(kaipan_reason_rows)
    legacy_reason_candidates = _reason_candidates(legacy_reason_rows)
    reason_candidates = dict(legacy_reason_candidates)
    reason_candidates.update(kaipan_asof_candidates if kaipan_reasons_by_symbol is not None
                             else _reason_candidates(kaipan_reason_rows))
    symbols = set(map(str, auction_rows)) | set(static_assignment.assignments) | set(pool_symbols) | set(reason_candidates)
    default_hierarchy = default_alias_hierarchy()
    kaipan_hierarchy = kaipan_hierarchy or PlateHierarchy({}, {})
    # Static alias edges are canonical and take precedence over Kaipan evidence.
    parent_by_child = dict(kaipan_hierarchy.parent_by_child)
    source_by_child = dict(kaipan_hierarchy.source_by_child)
    evidence_by_child = dict(kaipan_hierarchy.evidence_by_child)
    parent_by_child.update(default_hierarchy.parent_by_child)
    source_by_child.update(default_hierarchy.source_by_child)
    for child in default_hierarchy.parent_by_child:
        evidence_by_child.pop(child, None)
    hierarchy = PlateHierarchy(parent_by_child, source_by_child, evidence_by_child)
    memberships = _tag_reason_memberships(build_memberships(
        symbols=symbols, pool_plate_by_symbol=pool_plates,
        reason_candidates_by_symbol=reason_candidates,
        static_themes_by_symbol=static_themes, hot_plate_names=hot_plate_names,
        policy=MEMBERSHIP_POLICY_USER_PRIORITY_V1,
    ), kaipan_reason_rows)
    memberships = apply_hierarchy(memberships, hierarchy)
    legacy = _tag_reason_memberships(build_memberships(
        symbols=symbols, pool_plate_by_symbol=pool_plates,
        reason_candidates_by_symbol=reason_candidates,
        static_themes_by_symbol=static_themes, hot_plate_names=hot_plate_names,
        policy=MEMBERSHIP_POLICY_LEGACY_V1,
    ), kaipan_reason_rows)
    legacy = apply_hierarchy(legacy, hierarchy)

    facts: dict[str, StockFact] = {}
    missing = Counter()
    for symbol, raw in auction_rows.items():
        prior = current_to_previous.get(str(symbol))
        pre_close = _number(prior[4]) if prior is not None else None
        if pre_close is not None and pre_close <= 0:
            pre_close = None
        px_milli = _number(raw.get("px_milli"))
        price = px_milli / 1000.0 if px_milli is not None and px_milli > 0 else None
        amount = _number(raw.get("match_amt_yuan"))
        state_value = _number(raw.get("limit_state"))
        state = int(state_value) if state_value is not None and state_value.is_integer() else None
        amount_prev = _number(auction_0924_rows.get(str(symbol), {}).get("match_amt_yuan"))
        for key, value in (("pre_close", pre_close), ("price", price), ("match_amt_yuan", amount),
                           ("limit_state", state), ("amount_prev", amount_prev)):
            if value is None:
                missing[key] += 1
        facts[str(symbol)] = StockFact(
            symbol=str(symbol), pre_close=pre_close, price=price, amount=amount,
            limit_state=state, lb_days=pool_lb_days.get(str(symbol)), in_yest_pool=str(symbol) in pool_symbols,
            amount_prev=amount_prev,
        )

    aggregation = aggregate_plates(memberships, hierarchy, facts)
    membership_primary = {symbol: rows[0].plate for symbol, rows in memberships.by_symbol.items() if rows}
    legacy_primary = {symbol: rows[0].plate for symbol, rows in legacy.by_symbol.items() if rows}
    comparable = set(membership_primary) & set(legacy_primary)
    equal_count = sum(membership_primary[symbol] == legacy_primary[symbol] for symbol in comparable)
    no_plate = sum(not memberships.by_symbol.get(str(symbol)) for symbol in auction_rows)
    generic_count = sum(row.generic for row in aggregation.stats)
    symbol_flags: dict[str, list[str]] = {}
    for symbol in sorted(symbols):
        flags: set[str] = set()
        if not pool_symbols and not valid_kaipan_pool:
            flags.add("POOL_MISSING")
        if pool_source == "td_kline" and symbol in pool_symbols:
            flags.add("PRICE_ONLY_UNSEALED")
        if kaipan_pool_partial:
            flags.add("SOURCE_PARTIAL")
        if symbol not in kaipan_asof_candidates and symbol not in legacy_reason_candidates:
            flags.add("REASON_MISSING")
        if snapshot_asof is None or (reason_asof_unknown and symbol in legacy_reason_candidates):
            flags.add("ASOF_UNKNOWN")
        if snapshot_asof is None or trade_date < snapshot_asof:
            flags.add("NOT_POINT_IN_TIME")
        if not hot_plate_names:
            flags.add("HOT_MISSING")
        if symbol not in static_themes:
            flags.add("STATIC_MISSING")
        if symbol in pool_symbols and pool_lb_days.get(symbol) is None:
            flags.add("LB_UNKNOWN")
        symbol_flags[symbol] = sorted(flags)
    degradation: dict[str, Any] = {
        "POOL_MISSING": not pool_symbols and not valid_kaipan_pool,
        "PRICE_ONLY_UNSEALED": sum("PRICE_ONLY_UNSEALED" in flags for flags in symbol_flags.values()),
        "REASON_MISSING": sum("REASON_MISSING" in flags for flags in symbol_flags.values()),
        "ASOF_UNKNOWN": sum("ASOF_UNKNOWN" in flags for flags in symbol_flags.values()),
        "HOT_MISSING": sum("HOT_MISSING" in flags for flags in symbol_flags.values()),
        "STATIC_MISSING": sum("STATIC_MISSING" in flags for flags in symbol_flags.values()),
        "SOURCE_PARTIAL": bool(kaipan_pool_partial),
        "LB_UNKNOWN": sum("LB_UNKNOWN" in flags for flags in symbol_flags.values()),
        "NOT_POINT_IN_TIME": sum("NOT_POINT_IN_TIME" in flags for flags in symbol_flags.values()),
    }
    quality = {
        "trade_date": trade_date,
        "asof_semantics": "REPLAY_FROM_DATED_REASONS_NOT_AVAILABLE_AT",
        "snapshot_asof": snapshot_asof,
        "conclusion_phase": conclusion_phase,
        "usage_restriction": ("仅用于收盘后板块归类, 不得作为09:20-09:32结论输入"
                              if conclusion_phase == "EOD" else None),
        "reason_asof_rule": "Date<=D" if conclusion_phase == "EOD" else "Date<D",
        "reason_item_count_excluded_same_day": reason_counts["excluded_same_day"],
        "flags": ["NOT_POINT_IN_TIME"] if snapshot_asof is None or trade_date < snapshot_asof else [],
        "degradation": degradation,
        "hot_plate_source": "kaipan_his_plates" if hot_plate_input else "missing",
        "hot_plate_count": len(hot_plate_rows),
        "hot_plate_matched_member_count": len({
            row.plate for rows in memberships.by_symbol.values() for row in rows
            if row.plate in set(hot_plate_names)
        }),
        "invalid_hot_plate_rows": int(hot_plate_input.get("invalid_rows", 0)) if hot_plate_input else 0,
        "empty_hot_plate_name_count": int(hot_plate_input.get("empty_names", 0)) if hot_plate_input else 0,
        "auction_row_count": len(auction_rows), "membership_symbol_count": len(memberships.by_symbol),
        "yest_pool_member_count": len(pool_symbols), "mapped_auction_symbol_count": len(auction_rows) - no_plate,
        "unmapped_auction_symbol_count": no_plate,
        "auction_coverage": (len(auction_rows) - no_plate) / len(auction_rows) if auction_rows else None,
        "membership_clipped_count": 0,
        "generic_plate_count": generic_count, "plate_count": len(aggregation.stats),
        "generic_plate_share": generic_count / len(aggregation.stats) if aggregation.stats else None,
        "missing_counts": dict(sorted(missing.items())),
        "skipped_kline_table_symbol_count": skipped_kline_table_symbol_count,
        "previous_kline_date": prior_day, "pool_derivation_hash": derivation_hash,
        "pool_source": pool_source,
        "kaipan_pool_member_count": len(selected_pool_rows) if pool_source.startswith("kaipan_") else 0,
        "td_pool_member_difference_count": len(pool_symbols ^ td_pool_symbols),
        "kaipan_reasons_only": len(set(reason_pool_rows) - td_pool_symbols),
        "td_only": len(td_pool_symbols - set(reason_pool_rows)),
        "hierarchy_edge_count": len(kaipan_hierarchy.parent_by_child),
        "hierarchy_items_used": hierarchy_counts["used"],
        "hierarchy_items_excluded_future": hierarchy_counts["excluded_future"],
        "hierarchy_items_excluded_no_date": hierarchy_counts["excluded_no_date"],
        "reason_item_count_used": reason_counts["used"],
        "reason_item_count_type0": reason_counts["type0"],
        "reason_item_count_type1": reason_counts["type1"],
        "reason_item_count_excluded_future": reason_counts["excluded_future"],
        "reason_item_count_excluded_no_date": reason_counts["excluded_no_date"],
        "reason_symbol_count_used": reason_counts["symbol_count_used"],
        "reason_symbol_count_asof_unavailable": len(unavailable_symbols),
        "reason_source_counts": {
            "kaipan_asof": (len(kaipan_reason_rows)
                             if kaipan_reasons_by_symbol is not None else
                             len(_reason_candidates(kaipan_reason_rows))),
            "kaipan_unavailable_asof": len(unavailable_symbols),
            "legacy_snapshot": (len(legacy_reason_candidates) if kaipan_reasons_by_symbol is not None
                                else len(legacy_reason_candidates)),
        },
        "reason_asof_unknown_symbol_count": (len(legacy_reason_candidates)
                                              if kaipan_reasons_by_symbol is not None else
                                              (len(legacy_reason_candidates) if reason_asof_unknown else 0)),
        "hierarchy_static_conflict_count": len(set(kaipan_hierarchy.parent_by_child) & set(default_hierarchy.parent_by_child)),
        "hierarchy_static_conflicts": {
            child: {"kaipan_parent": kaipan_hierarchy.parent_by_child[child],
                    "static_parent": default_hierarchy.parent_by_child[child]}
            for child in sorted(set(kaipan_hierarchy.parent_by_child) & set(default_hierarchy.parent_by_child))
        },
        "kaipan_hierarchy_evidence": dict(kaipan_hierarchy.evidence_by_child),
        "legacy_v1_primary_count": len(legacy_primary),
        "legacy_v1_primary_match_count": equal_count,
        "legacy_v1_primary_comparable_count": len(comparable),
        "legacy_v1_primary_match_rate": equal_count / len(comparable) if comparable else None,
    }
    yest_pool_members = {
        symbol: {"tier": 1, "source": pool_source, "plate": pool_plates.get(symbol),
                 "lb_days": pool_lb_days.get(symbol)}
        for symbol in sorted(pool_symbols)
    }
    hot_plate_metadata_by_name = {}
    for rank, row in enumerate(hot_plate_rows, start=1):
        hot_plate_metadata_by_name.setdefault(row["name"], {"hot_rank": rank, "hot_code": row["code"]})
    return {"memberships": memberships, "legacy_v1_primary_by_symbol": legacy_primary,
            "aggregation": aggregation, "quality": quality,
            "reason_asof_unknown": (reason_asof_unknown if kaipan_reasons_by_symbol is None else
                                     bool(quality["reason_source_counts"]["legacy_snapshot"])),
            "yest_pool_symbols": pool_symbols,
            "yest_pool_members": yest_pool_members,
            "hot_plates": [{**row, "asof_date": prior_day} for row in hot_plate_rows],
            "hot_plate_metadata_by_name": hot_plate_metadata_by_name,
            "symbol_flags": symbol_flags}


def run_pipeline(
    *, dates: Sequence[str], klines_by_symbol: Mapping[str, Sequence[Sequence[Any]]],
    auction_by_date: Mapping[str, Mapping[str, Mapping[str, Any]]], snapshot: Mapping[str, Any],
    reasons_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]], start_date: str, end_date: str,
    reason_asof_unknown: bool = False, auction_0924_by_date: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    min_auction_rows: int = 4000, skipped_kline_table_symbol_count: int = 0,
    kaipan_pool_by_day: Mapping[str, Mapping[str, PoolRow]] | None = None,
    kaipan_pool_partial_dates: set[str] | None = None,
    kaipan_hierarchy: PlateHierarchy | None = None,
    kaipan_reasons_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    kaipan_reason_items: Sequence[Any] | None = None,
    kaipan_hot_plates_by_day: Mapping[str, Mapping[str, Any]] | None = None,
    snapshot_asof: str | None = None,
    conclusion_phase: str = "AUCTION",
) -> dict[str, Any]:
    if kaipan_reason_items is None and kaipan_reasons_by_symbol is not None:
        kaipan_reason_items = parse_kaipan_reason_items([
            {"StockID": str(symbol), "List": list(rows)}
            for symbol, rows in kaipan_reasons_by_symbol.items()
        ])
    included, skipped = [], []
    for day in dates:
        if not start_date <= day <= end_date:
            continue
        auction = auction_by_date.get(day, {})
        if len(auction) < min_auction_rows:
            skipped.append({"date": day, "reason": "auction_row_count_below_minimum", "auction_row_count": len(auction)})
            continue
        prior_day = max((session for session in _kline_by_day(klines_by_symbol) if session < day), default="")
        included.append((day, build_day(
            trade_date=day, klines_by_symbol=klines_by_symbol, auction_rows=auction, snapshot=snapshot,
            reasons_by_symbol=reasons_by_symbol, reason_asof_unknown=reason_asof_unknown,
            auction_0924_rows=(auction_0924_by_date or {}).get(day, {}), min_auction_rows=min_auction_rows,
            skipped_kline_table_symbol_count=skipped_kline_table_symbol_count,
            kaipan_pool_rows=(kaipan_pool_by_day or {}).get(
                prior_day,
            ), kaipan_hierarchy=kaipan_hierarchy,
            kaipan_pool_partial=prior_day in (kaipan_pool_partial_dates or set()),
            kaipan_reasons_by_symbol=kaipan_reasons_by_symbol,
            kaipan_reason_items=kaipan_reason_items,
            hot_plate_input=(kaipan_hot_plates_by_day or {}).get(prior_day),
            snapshot_asof=snapshot_asof,
            conclusion_phase=conclusion_phase,
        )))
    return {"days": included, "skipped": skipped}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_snapshot(path: Path) -> dict[str, Any]:
    if path.is_dir():
        candidates = (path / "legacy_snapshot.json", path / "snapshot.json")
        for candidate in candidates:
            if candidate.exists():
                raw = _read_json(candidate)
                if "stock_plate" in raw and "s2p" in raw:
                    return raw
        stock_path, s2p_path = path / "stock_plate.json", path / "s2p.json"
        if stock_path.exists() and s2p_path.exists():
            return {"stock_plate": _read_json(stock_path), "s2p": _read_json(s2p_path)}
        raise ValueError("snapshot directory must contain stock_plate.json and s2p.json or legacy_snapshot.json")
    raw = _read_json(path)
    if "stock_plate" not in raw or "s2p" not in raw:
        raise ValueError("snapshot JSON must contain stock_plate and s2p")
    return raw


def _load_reasons(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    if path is None:
        return {}
    raw = _read_json(path)
    if isinstance(raw, Mapping):
        def _rows(value: Any) -> list[dict[str, Any]]:
            items = value if isinstance(value, list) else [value]
            return [dict(i) if isinstance(i, Mapping) else {"reason": str(i)} for i in items]
        return {str(symbol): _rows(rows) for symbol, rows in raw.items()}
    result: dict[str, list[dict[str, Any]]] = {}
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        symbol = row.get("symbol") or row.get("field")
        if symbol is not None:
            value = row.get("value")
            if isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except ValueError:
                    decoded = {"reason": value}
                row = decoded if isinstance(decoded, Mapping) else {"reason": value}
            result.setdefault(str(symbol), []).append(dict(row))
    return result


def _load_kaipan_inputs(path: Path) -> tuple[dict[str, dict[str, PoolRow]], PlateHierarchy]:
    pools, hierarchy, _partial_dates = _load_kaipan_inputs_with_status(path)
    return pools, hierarchy


def _load_kaipan_inputs_with_status(
    path: Path, *, parse_hierarchy: bool = True,
) -> tuple[dict[str, dict[str, PoolRow]], PlateHierarchy, set[str]]:
    candidates: dict[str, dict[str, PoolRow]] = {}
    files_by_date: dict[str, set[int]] = {}
    invalid_dates: set[str] = set()
    pool_dir = path / "pools"
    if pool_dir.is_dir():
        for source in sorted(pool_dir.glob("*_ban*.json")):
            match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})_ban([1-5])\.json", source.name)
            if not match:
                continue
            trade_date, level_text = match.groups()
            files_by_date.setdefault(trade_date, set()).add(int(level_text))
            try:
                raw_pool = _read_json(source)
            except (OSError, ValueError):
                invalid_dates.add(trade_date)
                continue
            if not isinstance(raw_pool, Mapping) or not isinstance(raw_pool.get("info"), (list, tuple)):
                invalid_dates.add(trade_date)
                continue
            parsed = parse_kaipan_pool(raw_pool)
            current = candidates.setdefault(trade_date, {})
            for symbol, row in parsed.items():
                existing = current.get(symbol)
                if existing is None or (row.lb_days is not None and
                                        (existing.lb_days is None or row.lb_days > existing.lb_days)):
                    current[symbol] = row
    manifest: Mapping[str, Any] = {}
    manifest_path = path / "manifest.json"
    if manifest_path.is_file():
        try:
            raw_manifest = _read_json(manifest_path)
        except (OSError, ValueError):
            raw_manifest = {}
        if isinstance(raw_manifest, Mapping):
            manifest = raw_manifest
    pool_complete_by_date = manifest.get("pool_complete_by_date", {})
    successful_dates = {trade_date for trade_date, complete in pool_complete_by_date.items() if complete is True} \
        if isinstance(pool_complete_by_date, Mapping) else set()
    requested_dates = set(manifest.get("parameters", {}).get("dates", ())) \
        if isinstance(manifest.get("parameters"), Mapping) else set()
    pools: dict[str, dict[str, PoolRow]] = {}
    partial_dates: set[str] = set()
    for trade_date, candidate_rows in candidates.items():
        files_complete_with_data = (files_by_date.get(trade_date) == {1, 2, 3, 4, 5}
                                    and bool(candidate_rows) and trade_date not in invalid_dates)
        manifest_confirms = trade_date in successful_dates and trade_date not in invalid_dates
        if files_complete_with_data or manifest_confirms:
            pools[trade_date] = candidate_rows
        else:
            partial_dates.add(trade_date)
    partial_dates.update((files_by_date.keys() | requested_dates) - pools.keys())
    reason_responses = [_read_json(source) for source in sorted((path / "reasons").glob("*.json"))] \
        if (path / "reasons").is_dir() else []
    hierarchy = parse_kaipan_reason_hierarchy(reason_responses) if parse_hierarchy else PlateHierarchy({}, {})
    return pools, hierarchy, partial_dates


def _load_kaipan_reasons(path: Path) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    reason_dir = path / "reasons"
    if not reason_dir.is_dir():
        return result
    for source in sorted(reason_dir.glob("*.json")):
        raw = _read_json(source)
        if not isinstance(raw, Mapping):
            result[source.stem] = []
            continue
        symbol = str(raw.get("StockID") or raw.get("symbol") or source.stem)
        items = raw.get("List", raw.get("list", []))
        if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
            result[symbol] = [dict(item) for item in items if isinstance(item, Mapping)]
        else:
            result[symbol] = []
    return result


def _load_kaipan_hot_plates(path: Path) -> dict[str, dict[str, Any]]:
    """Load normalized Kaipan historical hot-plate rows partitioned by date."""
    result: dict[str, dict[str, Any]] = {}
    hot_dir = path / "hot_plates"
    if not hot_dir.is_dir():
        return result
    for source in sorted(hot_dir.glob("*.json")):
        try:
            date.fromisoformat(source.stem)
        except ValueError:
            continue
        raw = _read_json(source)
        raw_rows = raw.get("list") if isinstance(raw, Mapping) else None
        if not isinstance(raw_rows, list):
            result[source.stem] = {"rows": [], "invalid_rows": 1, "empty_names": 0}
            continue
        rows, invalid, empty_names = [], 0, 0
        for row in raw_rows:
            if (not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < 4
                    or not isinstance(row[0], (str, int)) or isinstance(row[0], bool)
                    or _number(row[2]) is None or _number(row[3]) is None):
                invalid += 1
                continue
            name = normalize_plate_name(row[1])
            if not name:
                empty_names += 1
                continue
            rows.append({"rank": len(rows) + 1, "code": str(row[0]), "name": name,
                         "strength": _number(row[2]), "change_pct": _number(row[3])})
        result[source.stem] = {"rows": rows, "invalid_rows": invalid, "empty_names": empty_names}
    return result


def _snapshot_asof(path: Path, explicit: str | None, snapshot: Mapping[str, Any]) -> str | None:
    if explicit:
        try:
            return date.fromisoformat(explicit).isoformat()
        except ValueError as exc:
            raise ValueError("snapshot-asof must be YYYY-MM-DD") from exc
    manifest_path = (path / "manifest.json") if path.is_dir() else path.parent / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = _read_json(manifest_path)
        except (OSError, ValueError, json.JSONDecodeError):
            manifest = {}
        if isinstance(manifest, Mapping):
            for key in ("snapshot_asof", "asof_date", "trade_date"):
                value = manifest.get(key)
                if isinstance(value, str):
                    try:
                        return date.fromisoformat(value[:10]).isoformat()
                    except ValueError:
                        pass
    for key in ("snapshot_asof", "asof_date", "trade_date"):
        value = snapshot.get(key)
        if isinstance(value, str):
            try:
                return date.fromisoformat(value).isoformat()
            except ValueError:
                pass
    sources = list(path.iterdir()) if path.is_dir() else [path]
    for source in [path, *sources]:
        match = re.search(r"(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)", source.name)
        if match:
            try:
                return date.fromisoformat(match.group(1)).isoformat()
            except ValueError:
                pass
    return None


def _snapshot_sealed_at(path: Path, explicit: str | None, snapshot: Mapping[str, Any]) -> datetime | None:
    """Resolve snapshot sealing time; dated snapshots default to 15:10 Shanghai time."""
    manifest_path = (path / "manifest.json") if path.is_dir() else path.parent / "manifest.json"
    manifest: Mapping[str, Any] = {}
    if manifest_path.is_file():
        try:
            raw = _read_json(manifest_path)
            if isinstance(raw, Mapping):
                manifest = raw
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    for key in ("snapshot_sealed_at", "snapshot_asof_at", "sealed_at", "captured_at", "snapshot_asof"):
        value = manifest.get(key)
        if isinstance(value, str) and ("T" in value or " " in value):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed.astimezone(_SHANGHAI) if parsed.tzinfo else parsed.replace(tzinfo=_SHANGHAI)
            except ValueError:
                pass
    asof = _snapshot_asof(path, explicit, snapshot)
    if asof is None:
        return None
    return datetime.fromisoformat(f"{asof}T15:10:00").replace(tzinfo=_SHANGHAI)


def _connect_td(host: str):
    try:
        import taos  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("TD query requires the 'taos' Python client") from exc
    database = os.environ.get("TDENGINE_DATABASE", "market_data1")
    if not database.replace("_", "").isalnum():
        raise ValueError("invalid TDENGINE_DATABASE")
    return taos.connect(
        host=host, port=int(os.environ.get("TDENGINE_PORT", "6030")),
        user=os.environ.get("TDENGINE_USER", "root"), password=os.environ.get("TDENGINE_PASSWORD", "taosdata"),
        database=database, config=os.environ.get("TDENGINE_CONFIG", "/etc/taos"),
    ), database


def _is_missing_table_error(exc: Exception) -> bool:
    message = str(exc).lower()
    if "table does not exist" in message or "0x2603" in message:
        return True
    errno = getattr(exc, "errno", None)
    try:
        return int(errno, 0) == 0x2603 if isinstance(errno, str) else int(errno) == 0x2603
    except (TypeError, ValueError, OverflowError):
        return False


def _read_td(host: str, database: str, symbols: Sequence[str], kline_start: str, kline_end: str,
             auction_dates: Sequence[str]) -> tuple[
                 dict[str, list[Sequence[Any]]], dict[str, dict[str, dict[str, Any]]],
                 dict[str, dict[str, dict[str, Any]]], int,
             ]:
    connection, actual_database = _connect_td(host)
    if database != actual_database:
        raise ValueError("TD database configuration changed during connection")
    try:
        cursor = connection.cursor()
        klines: dict[str, list[Sequence[Any]]] = {}
        skipped_kline_table_symbol_count = 0
        for symbol in symbols:
            if not re.fullmatch(r"\d{6}", symbol):
                continue
            table = "d_" + symbol
            try:
                cursor.execute(
                    f"SELECT ts, open, high, low, close, volume, amount, turnover, pct_chg FROM {table} "
                    f"WHERE ts >= '{kline_start}' AND ts <= '{kline_end}' ORDER BY ts"
                )
                klines[symbol] = list(cursor.fetchall())
            except Exception as exc:
                if not _is_missing_table_error(exc):
                    raise
                skipped_kline_table_symbol_count += 1
        auction: dict[str, dict[str, dict[str, Any]]] = {}
        previous: dict[str, dict[str, dict[str, Any]]] = {}
        for day in auction_dates:
            compact = day.replace("-", "")
            for tag, target in (("0925", auction), ("0924", previous)):
                cursor.execute(
                    "SELECT symbol, px_milli, match_amt_yuan, limit_state "
                    f'FROM {actual_database}.auction_snapshot_v2 WHERE trade_date="{compact}" AND auction_tag="{tag}"'
                )
                target[day] = {
                    str(symbol): {"px_milli": px, "match_amt_yuan": amount, "limit_state": state}
                    for symbol, px, amount, state in cursor.fetchall()
                }
        return klines, auction, previous, skipped_kline_table_symbol_count
    finally:
        connection.close()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _legacy_pool_symbols(path: Path, day: str) -> set[str] | None:
    candidates = (path / f"{day}.json", path / day / "yest_limit_pool.json",
                  path / day / "legacy_pool.json", path / "yest_limit_pool.json")
    source = next((candidate for candidate in candidates if candidate.exists()), None)
    if source is None:
        return None
    raw = _read_json(source)
    if isinstance(raw, Mapping):
        raw = raw.get("rows", raw.get("yest_limit_pool", list(raw.values())))
    symbols: set[str] = set()
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for row in raw:
            if isinstance(row, Mapping) and row.get("symbol") is not None:
                symbols.add(str(row["symbol"]))
            elif isinstance(row, str):
                symbols.add(row)
    return symbols


def _emit(output_dir: Path, result: Mapping[str, Any], args: argparse.Namespace) -> None:
    daily_rows = result["days"]
    plate_days: Counter[str] = Counter()
    hashes: dict[str, str] = {}
    unmapped: dict[str, int] = {}
    skipped_kline_symbols: dict[str, int] = {}
    same_matches = same_comparable = 0
    legacy_pool_comparison: dict[str, Any] = {}
    for day, value in daily_rows:
        folder = output_dir / day
        folder.mkdir()
        membership_payload = _jsonable_memberships(
            value["memberships"], value.get("hot_plate_metadata_by_name"),
        )
        membership_payload["legacy_v1_primary_by_symbol"] = value["legacy_v1_primary_by_symbol"]
        membership_payload["conclusion_phase"] = value["quality"].get("conclusion_phase", "AUCTION")
        if membership_payload["conclusion_phase"] == "EOD":
            membership_payload["usage_restriction"] = "仅用于收盘后板块归类, 不得作为09:20-09:32结论输入"
        membership_payload["yest_pool_symbols"] = sorted(value["yest_pool_symbols"])
        membership_payload["yest_pool_members"] = value["yest_pool_members"]
        membership_payload["symbol_flags"] = value["symbol_flags"]
        membership_payload["engine_membership_content_hash"] = membership_payload["content_hash"]
        canonical_membership_payload = json.dumps(
            membership_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        membership_payload["content_hash"] = hashlib.sha256(canonical_membership_payload.encode("utf-8")).hexdigest()
        _write_json(folder / "memberships.json", membership_payload)
        _write_json(folder / "plates.json", _jsonable_aggregation(value["aggregation"]))
        quality = dict(value["quality"])
        quality["reason_asof_unknown"] = value["reason_asof_unknown"]
        quality["conclusion_phase"] = value["quality"].get("conclusion_phase", "AUCTION")
        if quality["conclusion_phase"] == "EOD":
            quality["usage_restriction"] = "仅用于收盘后板块归类, 不得作为09:20-09:32结论输入"
        _write_json(folder / "day_quality.json", quality)
        _write_json(folder / "hot_plates.json", {
            "asof_date": (value["hot_plates"][0]["asof_date"] if value["hot_plates"] else
                          quality.get("previous_kline_date")),
            "rows": value["hot_plates"],
        })
        hashes[day] = membership_payload["content_hash"]
        unmapped[day] = quality["unmapped_auction_symbol_count"]
        skipped_kline_symbols[day] = quality["skipped_kline_table_symbol_count"]
        same_matches += quality["legacy_v1_primary_match_count"]
        same_comparable += quality["legacy_v1_primary_comparable_count"]
        for plate in {row.plate for rows in value["memberships"].by_symbol.values() for row in rows}:
            plate_days[plate] += 1
        legacy_dir = getattr(args, "legacy_pool_dir", None)
        if legacy_dir is not None:
            legacy_symbols = _legacy_pool_symbols(legacy_dir, day)
            if legacy_symbols is None:
                legacy_pool_comparison[day] = {"status": "MISSING_LEGACY_FILE"}
            else:
                derived = value["yest_pool_symbols"]
                legacy_pool_comparison[day] = {
                    "status": "COMPARED", "legacy_member_count": len(legacy_symbols),
                    "derived_member_count": value["quality"]["yest_pool_member_count"],
                    "intersection_count": len(derived & legacy_symbols) if derived else None,
                }
    summary = {
        "start_date": args.start_date, "end_date": args.end_date,
        "included_dates": [day for day, _ in daily_rows], "skipped_dates": result["skipped"],
        "membership_content_hash_by_date": hashes,
        "plate_appearance_days": dict(sorted(plate_days.items())),
        "legacy_v1_primary_match_count": same_matches,
        "legacy_v1_primary_comparable_count": same_comparable,
        "legacy_v1_primary_match_rate": same_matches / same_comparable if same_comparable else None,
        "unmapped_auction_symbol_count_by_date": unmapped,
        "skipped_kline_table_symbol_count_by_date": skipped_kline_symbols,
        "legacy_pool_comparison": legacy_pool_comparison if getattr(args, "legacy_pool_dir", None) else None,
        "asof_semantics": "REPLAY_FROM_DATED_REASONS_NOT_AVAILABLE_AT",
        "snapshot_asof": getattr(args, "snapshot_asof", None),
        "conclusion_phase": getattr(args, "conclusion_phase", "AUCTION"),
        "usage_restriction": ("仅用于收盘后板块归类, 不得作为09:20-09:32结论输入"
                              if getattr(args, "conclusion_phase", "AUCTION") == "EOD" else None),
        "reason_asof_rule": "Date<=D" if getattr(args, "conclusion_phase", "AUCTION") == "EOD" else "Date<D",
        "reason_item_count_excluded_same_day_by_date": {
            day: value["quality"]["reason_item_count_excluded_same_day"] for day, value in daily_rows
        },
        "interpretation": "离线事实, 非策略结论",
        "reason_asof_unknown": bool(args.reasons),
    }
    _write_json(output_dir / "summary.json", summary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--snapshot-asof", help="static snapshot date YYYY-MM-DD when its name does not include one")
    parser.add_argument("--conclusion-phase", choices=("AUCTION", "EOD"), default="AUCTION")
    parser.add_argument("--reasons", type=Path)
    parser.add_argument("--kline-range", required=True, help="bounded TD date range YYYY-MM-DD:YYYY-MM-DD")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--td-host", default="127.0.0.1")
    parser.add_argument("--legacy-pool-dir", type=Path, help="optional read-only comparison input (reserved)")
    parser.add_argument("--kaipan-dir", type=Path, help="optional sealed Kaipan pools/ and reasons/ input")
    args = parser.parse_args(argv)
    start, end = date.fromisoformat(args.start_date), date.fromisoformat(args.end_date)
    try:
        kline_start_text, kline_end_text = args.kline_range.split(":", 1)
        kline_start, kline_end = date.fromisoformat(kline_start_text), date.fromisoformat(kline_end_text)
    except (ValueError, TypeError):
        parser.error("--kline-range must be YYYY-MM-DD:YYYY-MM-DD")
    if start > end or kline_start > kline_end or kline_start > start - timedelta(days=1) or kline_end < end:
        parser.error("date range must be ordered and kline range must cover a prior session through end date")
    ensure_empty_output_dir(args.output_dir)
    snapshot = _load_snapshot(args.snapshot)
    reasons = _load_reasons(args.reasons)
    kaipan_pools, kaipan_hierarchy, kaipan_pool_partial_dates = \
        _load_kaipan_inputs_with_status(args.kaipan_dir, parse_hierarchy=False) \
        if args.kaipan_dir else ({}, None, set())
    kaipan_reasons = _load_kaipan_reasons(args.kaipan_dir) if args.kaipan_dir else None
    kaipan_hot_plates = _load_kaipan_hot_plates(args.kaipan_dir) if args.kaipan_dir else {}
    symbols = sorted(set(map(str, snapshot["stock_plate"])) | set(map(str, snapshot["s2p"])) | set(reasons))
    auction_dates = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    # Confirm database value before opening and reuse that same value for the query.
    klines, auctions, previous, skipped_kline_symbols = _read_td(args.td_host, os.environ.get("TDENGINE_DATABASE", "market_data1"),
                                           symbols, kline_start.isoformat(), kline_end.isoformat(), auction_dates)
    snapshot_asof = _snapshot_asof(args.snapshot, args.snapshot_asof, snapshot)
    result = run_pipeline(
        dates=auction_dates, klines_by_symbol=klines, auction_by_date=auctions, snapshot=snapshot,
        reasons_by_symbol=reasons, start_date=args.start_date, end_date=args.end_date,
        reason_asof_unknown=args.reasons is not None, auction_0924_by_date=previous,
        skipped_kline_table_symbol_count=skipped_kline_symbols,
        kaipan_pool_by_day=kaipan_pools, kaipan_hierarchy=kaipan_hierarchy,
        kaipan_pool_partial_dates=kaipan_pool_partial_dates,
        kaipan_reasons_by_symbol=kaipan_reasons,
        kaipan_hot_plates_by_day=kaipan_hot_plates,
        snapshot_asof=snapshot_asof,
        conclusion_phase=args.conclusion_phase,
    )
    args.snapshot_asof = snapshot_asof
    _emit(args.output_dir, result, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
