#!/usr/bin/env python3
"""Run every scheduled event once through examples/guardian_handler.py in dry-run mode and check its reply contract.

Isolated: GUARDIAN_DRY_RUN=1, an empty temporary CORE_DATA_ROOT (so no catalog, no `.mail_enabled`), no TD_CONFIG, and
the read-only Redis proxy. It reads the shipped schedule and writes nothing outside the temporary directory.
Output: one JSON object per event on stdout, then a summary; exit 1 when any event violates the contract.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from engine_core.guardian.contract import check_reply  # noqa: E402

TRADE_DATE = "2026-11-03"


def run_event(event: dict, data_root: str, timeout: int) -> dict:
    request = {"event_id": event["event_id"], "handler": event["handler"], "trade_date": TRADE_DATE,
               "args": event.get("args") or {}}
    env = {**os.environ, "GUARDIAN_DRY_RUN": "1", "CORE_DATA_ROOT": data_root, "GUARDIAN_EMAIL_SELF": "0",
           "REDIS_URL": "redis://127.0.0.1:6380/0", "PYTHONPATH": str(ROOT / "src")}
    env.pop("TD_CONFIG", None)
    try:
        done = subprocess.run([sys.executable, str(ROOT / "examples" / "guardian_handler.py"), "run"],
                              input=json.dumps(request), capture_output=True, text=True, cwd=ROOT, env=env,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"event": event["event_id"], "handler": event["handler"], "exit": None, "violations": ["timeout"]}
    lines = done.stdout.strip().splitlines()
    try:
        reply = json.loads(lines[-1]) if lines else None
    except ValueError:
        reply = None
    return {"event": event["event_id"], "handler": event["handler"], "exit": done.returncode,
            "status": (reply or {}).get("status"), "error_code": (reply or {}).get("error_code"),
            "violations": check_reply(done.returncode, reply)}


def main() -> int:
    schedule = json.loads((ROOT / "src" / "engine_core" / "guardian" / "schedule.json").read_text(encoding="utf-8"))
    results = []
    with tempfile.TemporaryDirectory(prefix="c646_audit_") as data_root:
        for event in schedule["events"]:
            result = run_event(event, data_root, timeout=90)
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    bad = [item for item in results if item["violations"]]
    by_reason = Counter(reason for item in bad for reason in item["violations"])
    print(json.dumps({"summary": {"events": len(results), "violating": len(bad),
                                  "by_reason": dict(by_reason)}}, ensure_ascii=False))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
