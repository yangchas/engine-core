#!/usr/bin/env python3
"""Build immutable PIT plate amount baselines for 09:25 and 09:45."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from engine_core.plate_amount_baseline import build_and_store_plate_amount_baseline


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--artifact-root", default=os.getenv("CORE_DATA_ROOT", "~/core-data"))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if not args.write:
        parser.error("--write is required to materialize the baseline")
    result = build_and_store_plate_amount_baseline(args.date,
        artifact_root=Path(args.artifact_root).expanduser())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"READY", "PARTIAL"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
