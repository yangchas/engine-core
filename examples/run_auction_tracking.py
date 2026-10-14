#!/usr/bin/env python3
"""Replay frozen tick CSVs into an auction/opening timeline and fact events."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core.auction_tracking import (  # noqa: E402
    _load_json, _load_relations, _pool_symbols, load_ticks, replay_tracking,
    sha256_file, write_outputs,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--ticks-csv", required=True, type=Path)
    parser.add_argument("--open-ticks-csv", type=Path)
    parser.add_argument("--relation-dir", type=Path)
    parser.add_argument("--pool-json", type=Path)
    parser.add_argument("--baseline-json", type=Path)
    parser.add_argument("--prev-close-json", type=Path)
    parser.add_argument("--generic-names-json", type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if not args.ticks_csv.is_file():
            raise ValueError("ticks-csv does not exist")
        if args.open_ticks_csv and not args.open_ticks_csv.is_file():
            raise ValueError("open-ticks-csv does not exist")
        members, names, generic = _load_relations(args.relation_dir)
        if args.generic_names_json:
            raw_generic = _load_json(args.generic_names_json, [])
            generic = set(raw_generic if isinstance(raw_generic, list) else raw_generic.get("names", []))
        pool = _pool_symbols(_load_json(args.pool_json, []))
        baseline = _load_json(args.baseline_json, {})
        prev_close = _load_json(args.prev_close_json, {})
        ticks = load_ticks(args.ticks_csv, args.trade_date)
        open_ticks = load_ticks(args.open_ticks_csv, args.trade_date) if args.open_ticks_csv else []
        for rows in (ticks, open_ticks):
            for index, tick in enumerate(rows):
                raw_close = prev_close.get(tick.symbol, prev_close.get(tick.symbol.lstrip("0"))) if isinstance(prev_close, dict) else None
                if isinstance(raw_close, dict):
                    raw_close = raw_close.get("prev_close_milli", raw_close.get("pc_milli"))
                if not tick.pc_milli and raw_close not in (None, ""):
                    rows[index] = replace(tick, pc_milli=int(float(raw_close)))
        report, events = replay_tracking(trade_date=args.trade_date, ticks=ticks, open_ticks=open_ticks,
                                        relations=members, plate_names=names, generic_names=generic,
                                        pool_symbols=pool, baseline=baseline, prev_close=prev_close)
        statuses = {}
        for event in events:
            status = event.get("price_status") or "NOT_APPLICABLE"
            statuses[status] = statuses.get(status, 0) + 1
        manifest = {
            "contract": "AuctionTrackingV1", "trade_date": args.trade_date,
            "inputs": {"ticks_csv_sha256": sha256_file(args.ticks_csv),
                       "open_ticks_csv_sha256": sha256_file(args.open_ticks_csv),
                       "relation_registry_sha256": sha256_file(args.relation_dir / "registry.json") if args.relation_dir else None,
                       "relation_csv_sha256": sha256_file(args.relation_dir / "relation.csv") if args.relation_dir else None,
                       "pool_json_sha256": sha256_file(args.pool_json),
                       "baseline_json_sha256": sha256_file(args.baseline_json),
                       "prev_close_json_sha256": sha256_file(args.prev_close_json),
                       "generic_names_json_sha256": sha256_file(args.generic_names_json)},
            "tick_count": len(ticks), "open_tick_count": len(open_ticks),
            "price_status_coverage": report["price_status_coverage"],
            "price_status_event_counts": statuses,
            "temporal_note": "Slices use only ticks with ts <= slice timestamp; terminal share is retrospective.",
        }
        write_outputs(args.out_dir, report, events, manifest)
        print(f"trade_date={args.trade_date} ticks={len(ticks)} open_ticks={len(open_ticks)} slices={len(report['timeline'])} events={len(events)}")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
