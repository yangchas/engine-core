#!/usr/bin/env python3
"""Trading calendar admin: status | init [--rehearse] | refresh | force.

init: no matrix file -> cold-start light from real sources. With --rehearse and an existing matrix, the file is
renamed to calendar.json.bak-<UTC stamp> first so initialization can be tested; if init fails the backup is restored.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core.data_pipeline.trade_calendar import TradeCalendarStore  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["status", "init", "refresh", "force"])
    parser.add_argument("--root", default=os.getenv("CORE_DATA_ROOT", "~/core-data"))
    parser.add_argument("--rehearse", action="store_true", help="init: rename an existing matrix aside first")
    args = parser.parse_args(argv)
    store = TradeCalendarStore(args.root)
    if args.command == "status":
        out = store.calendar_status()
    elif args.command == "refresh":
        out = store.refresh_if_needed()
    elif args.command == "force":
        out = store.force_refresh()
    else:
        backup = None
        if args.rehearse and store.path.exists():
            backup = store.path.with_name("calendar.json.bak-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
            store.path.rename(backup)
        out = store.initialize()
        out["backup"] = str(backup) if backup else None
        if backup and not store.path.exists():
            backup.rename(store.path)
            out["restored_backup"] = True
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0 if out.get("status") in {"CONFIRMED", "EXISTS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
