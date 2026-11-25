#!/usr/bin/env python3
"""Print the long-running TDengine queries and their owners (read-only; needs TD_CONFIG).  --threshold-seconds N"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from engine_core.guardian import td_resource_check  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold-seconds", type=int, default=td_resource_check.DEFAULT_THRESHOLD_SECONDS)
    args = parser.parse_args(argv)
    code, reply = td_resource_check.run({"args": {"td_check": {"threshold_seconds": args.threshold_seconds}}}, ".")
    print(json.dumps(reply, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
