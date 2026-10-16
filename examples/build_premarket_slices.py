#!/usr/bin/env python3
"""Build a date-scoped compact daily-history slice before live reports."""
from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path

from engine_core.live_session import load_daily_history_csv
from engine_core.slice_cache import build_daily_history_slice, sha256_file, write_daily_history_slice


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--daily-history", required=True, type=Path)
    parser.add_argument("--asof", default=None)
    parser.add_argument("--slice-root", type=Path, default=None)
    args = parser.parse_args(argv)
    datetime.strptime(args.trade_date, "%Y-%m-%d")
    asof = args.asof or datetime.now().isoformat(timespec="seconds")
    root = args.slice_root or Path(os.environ.get(
        "CORE_SLICE_ROOT", str(Path.home() / "core-data" / "slices"))).expanduser()
    rows = load_daily_history_csv(args.daily_history)
    data = build_daily_history_slice(rows, trade_date=args.trade_date,
                                     source_sha256=sha256_file(args.daily_history),
                                     source_path=str(args.daily_history), asof=asof)
    path = write_daily_history_slice(root, args.trade_date, data)
    print(f"trade_date={args.trade_date} rows={data['row_count']} symbols={data['symbol_count']} "
          f"slice={path} sha256={sha256_file(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
