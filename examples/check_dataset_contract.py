#!/usr/bin/env python3
"""Check one catalog partition against fw/design/24-DATASET-CONTRACT.md (read-only).

Exit 0 = all checks pass, 1 = at least one violation, 2 = usage/spec error.
stdout: machine-readable JSON (or text with --format text); stderr: human summary.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core.data_pipeline.dataset_contract import (  # noqa: E402
    check_partition, latest_trade_date, load_spec)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD or 'latest'")
    ap.add_argument("--root", default=os.path.expanduser("~/core-data"))
    ap.add_argument("--version", help="check this version instead of the best-quality one")
    ap.add_argument("--spec", help="spec JSON path (default: dataset_specs/<dataset>.json)")
    ap.add_argument("--format", choices=("json", "text"), default="json")
    ap.add_argument("--out", help="also write the JSON report to this file")
    args = ap.parse_args(argv)
    try:
        spec = load_spec(args.dataset, args.spec)
    except (OSError, ValueError) as exc:
        print(f"spec error: {exc}", file=sys.stderr)
        return 2
    date = args.date
    if date == "latest":
        date = latest_trade_date(args.root, args.dataset)
        if date is None:
            print(f"no partitions for {args.dataset} in {args.root}", file=sys.stderr)
            return 1
    report = check_partition(args.root, args.dataset, date, version=args.version, spec=spec)
    data = report.to_dict()
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    if args.format == "json":
        print(text)
        print(report.summary(), file=sys.stderr)
    else:
        print(report.summary())
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
