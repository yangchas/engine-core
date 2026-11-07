#!/usr/bin/env python3
"""Build immutable PIT plate amount baselines for 09:25 and 09:45."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from engine_core.plate_amount_baseline import build_and_store_plate_amount_baseline
from engine_core.data_pipeline.job_contract import contract_main, JobResult, emit


@contract_main("build_plate_amount_baseline", dataset="plate_amount_baseline")
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--artifact-root", default=os.getenv("CORE_DATA_ROOT", "~/core-data"))
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--available-at")
    parser.add_argument("--td-split-retry-depth", type=int, default=0)
    args = parser.parse_args(argv)
    started = time.monotonic()
    if not args.write:
        parser.error("--write is required to materialize the baseline")
    result = build_and_store_plate_amount_baseline(args.date,
        artifact_root=Path(args.artifact_root).expanduser(), available_at=args.available_at,
        td_split_retry_depth=args.td_split_retry_depth)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    reused = result.get("reused", False)
    status = "not_applicable" if reused else "ok" if result["status"] == "READY" else "degraded"
    detail = {"reason": "incomplete historical membership or observations",
              "date_count": result.get("date_count"), "membership_date_count": result.get("membership_date_count")} if status == "degraded" else {}
    return emit(JobResult(contract=1, job="build_plate_amount_baseline", trade_date=args.date,
                          status=status, tier=0, source="core_td_plate_amount", dataset="plate_amount_baseline",
                          version=result.get("version"), row_count=result.get("row_count"), coverage=None,
                          error_code="ALREADY_SATISFIED" if reused else "", retryable=False, attempts=1,
                          elapsed_ms=(time.monotonic() - started) * 1000, detail=detail))


if __name__ == "__main__":
    raise SystemExit(main())
