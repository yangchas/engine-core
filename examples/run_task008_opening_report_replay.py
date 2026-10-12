"""Render the opening (09:32) fact report from a runner result JSON.

Reads one local result file produced by ``run_q2frame_auction_engine_shadow``
with ``--include-opening``; no replay and no external access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core.market_opening_report import build_market_opening_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    raw = args.result.read_bytes()
    result = json.loads(raw)
    evidence = result.get("ordered", {}).get("opening_evidence", {}).get("OPENING_0932")
    if not isinstance(evidence, dict) or evidence.get("status") == "NOT_OBSERVED_IN_REPLAY_WINDOW":
        raise SystemExit("result has no observed OPENING_0932 evidence")
    notes = [
        "universe is the symbols observed in the frozen input only; full-market coverage is unproven",
        "historical available_at and Rabbit arrival order are unknown",
    ]
    report = build_market_opening_report(
        trade_date=args.trade_date, event_id="OPENING_0932",
        data_origin="REPLAY_FROZEN_Q2FRAME", opening_evidence=evidence,
        top_n=args.top_n, notes=notes,
    )
    args.out_dir.mkdir(parents=True, exist_ok=False)
    (args.out_dir / "report.txt").write_text(report.text, encoding="utf-8")
    (args.out_dir / "manifest.json").write_text(
        json.dumps({**report.as_mapping(), "result_sha256": hashlib.sha256(raw).hexdigest()},
                   ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report.as_mapping(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
