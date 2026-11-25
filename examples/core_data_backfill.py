#!/usr/bin/env python3
"""Bounded, resumable historical Core daily-bar backfill."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from engine_core.data_pipeline.jobs import JobRunner, RequestBudget
from engine_core.data_pipeline.ops import backfill_daily_kline, backfill_ladder_daily


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, choices=["daily_kline", "ladder_daily"])
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end", required=True)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--adapter", help="module:function returning adapter mapping")
    parser.add_argument("--artifact-root", default=os.getenv("CORE_DATA_ROOT", "~/core-data"))
    parser.add_argument("--td-reader", help="optional read-only module:function(trade_date)")
    parser.add_argument("--ladder-reference", default="/home/exedev/validation/ladder-emotion-study-20261015/out_final/ladder_daily.csv")
    args = parser.parse_args(argv)
    root = Path(args.artifact_root).expanduser()
    adapters = {}
    if args.adapter:
        module_name, sep, function = args.adapter.partition(":")
        if not sep: parser.error("--adapter must be module:function")
        adapters = getattr(__import__(module_name, fromlist=[function]), function)()
    runner = JobRunner(artifact_root=root, budget=RequestBudget(
        max_requests=args.max_requests or 30, min_interval_seconds=0))
    if args.dataset == "ladder_daily":
        report = backfill_ladder_daily(runner, args.start, args.end,
            reference_path=args.ladder_reference)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if not report["missing_daily_kline"] and report["reference_comparison"]["mismatch_days"] == 0 else 2
    fetch = adapters.get("daily_kline")
    if fetch is None:
        from examples.run_data_jobs import _default_adapters
        fetch = _default_adapters(str(root), os.getenv("ENGINE_LEGACY_ROOT", "/home/exedev/repos/stock-situation-runtime"),
                                  args.max_requests or 30, args.write).get("daily_kline")
    td_reader = None
    if args.td_reader:
        module_name, sep, function = args.td_reader.partition(":")
        if not sep: parser.error("--td-reader must be module:function")
        td_reader = getattr(__import__(module_name, fromlist=[function]), function)
    report = backfill_daily_kline(runner, args.start, args.end, fetch=fetch,
        max_requests=args.max_requests, write=args.write, td_reader=td_reader,
        ladder_reference=args.ladder_reference if args.dataset == "ladder_daily" else None)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if not report["remaining"] and all(not item["mismatch_count"] for item in report["td_reconciliation"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
