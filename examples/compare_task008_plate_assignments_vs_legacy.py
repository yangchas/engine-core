#!/usr/bin/env python3
"""Compare Core stock plate assignments with a sealed legacy snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from engine_core.plate_rules import collapse_runtime_primary_plate, is_generic_plate
from engine_core.stock_plate_assignment import PlateAssignments, build_stock_plate_assignments


DIFF_KEYS = (
    "primary_diff_generic", "primary_diff_alias", "themes_order_only",
    "themes_content", "missing_in_core", "missing_in_legacy",
)


def compare_assignments(core: PlateAssignments, legacy: Mapping[str, Any]) -> dict[str, Any]:
    old_primary = {str(k): v for k, v in legacy.get("stock_plate", {}).items()}
    old_themes = {str(k): v for k, v in legacy.get("s2p", {}).items()}
    core_map = core.assignments
    core_symbols, legacy_symbols = set(core_map), set(old_primary) | set(old_themes)
    shared = sorted(core_symbols & legacy_symbols)
    counts = {key: 0 for key in DIFF_KEYS}
    samples: list[dict[str, Any]] = []
    primary_equal = themes_equal = 0
    for symbol in shared:
        assignment = core_map[symbol]
        cp, lp = assignment.primary_plate, old_primary.get(symbol)
        ct, lt = tuple(assignment.themes), tuple(old_themes.get(symbol) or ())
        primary_category = None
        if cp == lp:
            primary_equal += 1
        elif cp is None or lp is None:
            primary_category = "primary_diff_generic" if is_generic_plate(str(cp or lp or "")) else "themes_content"
        elif is_generic_plate(str(cp)) or is_generic_plate(str(lp)):
            primary_category = "primary_diff_generic"
        elif collapse_runtime_primary_plate(str(cp)) == collapse_runtime_primary_plate(str(lp)):
            primary_category = "primary_diff_alias"
        else:
            primary_category = "themes_content"
        theme_category = None
        if ct == lt:
            themes_equal += 1
        elif len(ct) == len(lt) and set(ct) == set(lt):
            theme_category = "themes_order_only"
        else:
            theme_category = "themes_content"
        categories = [c for c in (primary_category, theme_category) if c]
        for category in categories:
            counts[category] += 1
        if categories and len(samples) < 20:
            samples.append({"symbol": symbol, "categories": categories, "core_primary": cp, "legacy_primary": lp,
                            "core_themes": list(ct), "legacy_themes": list(lt)})
    missing_core = sorted(legacy_symbols - core_symbols)
    missing_legacy = sorted(core_symbols - legacy_symbols)
    counts["missing_in_core"] = len(missing_core)
    counts["missing_in_legacy"] = len(missing_legacy)
    for symbol, category in [(s, "missing_in_core") for s in missing_core] + [(s, "missing_in_legacy") for s in missing_legacy]:
        if len(samples) >= 20:
            break
        samples.append({"symbol": symbol, "categories": [category]})
    return {"n_compared": len(shared), "primary_equal": primary_equal, "themes_equal": themes_equal,
            "diff_counts": counts, "samples": samples}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-snapshot", required=True, type=Path)
    parser.add_argument("--static-primary", type=Path, help="JSON object mapping symbol to primary plate")
    parser.add_argument("--static-themes", type=Path, help="JSON object mapping symbol to ordered theme list")
    parser.add_argument("--pool", type=Path, help='optional sealed pool artifact with data.rows')
    parser.add_argument("--reasons", type=Path, help="optional JSON object mapping symbol to reason text or row list")
    parser.add_argument("--trade-date", help="defaults to trade_date in legacy snapshot")
    args = parser.parse_args()
    snapshot = json.loads(args.legacy_snapshot.read_text(encoding="utf-8"))
    static_primary = json.loads(args.static_primary.read_text(encoding="utf-8")) if args.static_primary else snapshot.get("stock_plate", {})
    static_themes = json.loads(args.static_themes.read_text(encoding="utf-8")) if args.static_themes else snapshot.get("s2p", {})
    pool_rows = json.loads(args.pool.read_text(encoding="utf-8"))["data"]["rows"] if args.pool else []
    raw_reasons = json.loads(args.reasons.read_text(encoding="utf-8")) if args.reasons else {}
    reasons = {str(k): ([v] if isinstance(v, dict) else v if isinstance(v, list) else [{"reason": str(v)}]) for k, v in raw_reasons.items()}
    core = build_stock_plate_assignments(trade_date=args.trade_date or snapshot.get("trade_date", ""),
        static_primary_by_symbol=static_primary, static_themes_by_symbol=static_themes,
        pool_rows=pool_rows, reasons_by_symbol=reasons)
    print(json.dumps(compare_assignments(core, snapshot), ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
