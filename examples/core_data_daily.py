#!/usr/bin/env python3
"""Run one Core data phase. Cron examples are printed, never installed."""
from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from engine_core.data_pipeline.ops import run_daily_phase


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=["premarket", "intraday", "postmarket"])
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--td-write", action="store_true")
    parser.add_argument("--adapter", help="module:function returning adapter mapping")
    parser.add_argument("--max-requests", type=int, default=30)
    parser.add_argument("--artifact-root", default=os.getenv("CORE_DATA_ROOT", "~/core-data"))
    parser.add_argument("--no-lock", action="store_true")
    args = parser.parse_args(argv)
    if args.td_write and not args.write:
        parser.error("--td-write requires --write")
    if args.write and not os.getenv("CORE_REDIS_URL"):
        parser.error("--write requires CORE_REDIS_URL")
    root = Path(args.artifact_root).expanduser()
    from examples.run_data_jobs import _default_adapters
    adapters = _default_adapters(str(root), os.getenv("ENGINE_LEGACY_ROOT", "/home/exedev/repos/stock-situation-runtime"),
                                  args.max_requests, args.write)
    if args.adapter:
        module_name, sep, function = args.adapter.partition(":")
        if not sep: parser.error("--adapter must be module:function")
        adapters = getattr(__import__(module_name, fromlist=[function]), function)()
    redis = None
    if args.write and os.getenv("CORE_REDIS_URL"):
        import redis as redis_package
        redis = redis_package.Redis.from_url(os.environ["CORE_REDIS_URL"], decode_responses=True)
    trade_dates = None
    calendar_file = root / "trade_dates.json"
    if calendar_file.is_file():
        trade_dates = json.loads(calendar_file.read_text(encoding="utf-8"))
    result = run_daily_phase(args.phase, args.date, root=root, adapters=adapters,
        trading_dates=trade_dates, write=args.write, redis=redis,
        td_adapter=adapters.get("td_writer"), td_write=args.td_write, lock=not args.no_lock)
    result["recommended_cron"] = [
        "50 8 * * 1-5 core_data_daily.py --phase premarket --write",
        "*/5 9-15 * * 1-5 core_data_daily.py --phase intraday --write",
        "40 17 * * 1-5 core_data_daily.py --phase postmarket --write",
        "30 18 * * 1-5 core_data_daily.py --phase postmarket --write",
        "0 20 * * 1-5 core_data_daily.py --phase postmarket --write",
    ]
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"READY", "SKIPPED_NON_TRADING_DAY"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
