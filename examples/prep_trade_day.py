#!/usr/bin/env python3
"""Prepare and report the Core-owned inputs needed before one trading day."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from engine_core.prep_trade_day import build_prep_report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="target trading day")
    parser.add_argument("--write", action="store_true", help="run missing Core jobs and publish permitted Core cache views")
    parser.add_argument("--artifact-root", default=os.getenv("CORE_DATA_ROOT", "~/core-data"))
    args = parser.parse_args(argv)
    report = build_prep_report(args.artifact_root, args.date, write=args.write)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["status"] in {"READY", "PARTIAL"} else 1

if __name__ == "__main__":
    raise SystemExit(main())
