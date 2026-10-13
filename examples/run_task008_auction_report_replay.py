"""Compose the deterministic market auction fact report from frozen inputs.

Inputs are local files only: a Q2Frame JSONL (merged to a cutoff), the TD
auction rows JSONL, a symbol->plate snapshot and a sealed previous-day pool
JSON.  No Redis/TD/Rabbit access.  Output goes to a new directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from engine_core import (  # noqa: E402
    build_current_0925_rows,
    build_previous_day_limit_feedback,
    build_previous_day_limit_structure,
    build_theme_delta_rows,
    theme_weights_from_plate_mapping,
)
from engine_core.auction_final_recovery import PhaseZeroFinalRecovery  # noqa: E402
from engine_core.market_auction_report import build_market_auction_report  # noqa: E402
from engine_core.market_summary import derive_q2_auction_summary  # noqa: E402
from engine_core.q2 import normalize_q2  # noqa: E402
from engine_core.theme_auction_delta import build_theme_auction_delta_facts  # noqa: E402
from examples.sanity_task008_prev_limit_feedback import execute_previous_pool  # noqa: E402


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--q2frame", type=Path, required=True)
    parser.add_argument("--cutoff", default="09:27:10", help="local HH:MM:SS; frames at or before it are merged")
    parser.add_argument("--td-rows", type=Path, required=True)
    parser.add_argument("--plate-snapshot", type=Path, required=True)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--recover-final-from-phase-zero", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--event-id", default="AUCTION_0925")
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = _build_parser().parse_args()

    cutoff = int(datetime.fromisoformat(f"{args.trade_date}T{args.cutoff}").replace(
        tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
    recovery = PhaseZeroFinalRecovery(args.trade_date) if args.recover_final_from_phase_zero else None
    state: dict = {}
    with args.q2frame.open("r", encoding="utf-8") as handle:
        for line in handle:
            frame = json.loads(line)
            if frame["logical_ts_ms"] > cutoff:
                break
            updates = recovery.apply(frame["q2_updates"]) if recovery else frame["q2_updates"]
            for update in updates:
                state.setdefault(update["symbol"], {}).update(update)
    quotes = {symbol: normalize_q2(symbol, raw) for symbol, raw in state.items()}
    summary = derive_q2_auction_summary(
        quotes, trade_date=args.trade_date, source_id="q2frame-merged",
        source_table="Q2FrameV1:event_time_replay", observation_time_ms=cutoff,
        input_content_hash=_sha(args.q2frame),
    )
    td_rows = [json.loads(line) for line in args.td_rows.read_text(encoding="utf-8").splitlines() if line.strip()]
    previous = execute_previous_pool(json.loads(args.pool.read_text(encoding="utf-8")))
    current = build_current_0925_rows(args.trade_date, td_rows)
    feedback = build_previous_day_limit_feedback(previous, current.rows, current_trade_date=args.trade_date)
    structure = build_previous_day_limit_structure(previous)
    delta = build_theme_delta_rows(args.trade_date, td_rows)
    plates = json.loads(args.plate_snapshot.read_text(encoding="utf-8"))["mapping"]
    weights = theme_weights_from_plate_mapping(plates, [row["symbol"] for row in delta.rows])
    themes = build_theme_auction_delta_facts(delta.rows, weights)
    notes = [
        "theme weights use the primary plate only (secondary plate mapping not supplied)",
        "previous-day pool availability is unverified (previous-day data)",
        "change_pct_delta is missing where 0924 had no match price",
    ]
    if recovery is not None:
        notes.append("0925 anchors include %d symbols recovered from late ph==0 snapshots" % len(recovery.recovered))
    report = build_market_auction_report(
        trade_date=args.trade_date, event_id=args.event_id,
        data_origin="REPLAY_FROZEN_TD_AND_Q2FRAME", market_summary=summary,
        previous_limit_structure=structure, previous_limit_feedback=feedback,
        theme_facts=themes, top_n=10, notes=notes,
    )
    args.out_dir.mkdir(parents=True, exist_ok=False)
    (args.out_dir / "report.txt").write_text(report.text, encoding="utf-8")
    manifest = {
        **report.as_mapping(),
        "cutoff_local": args.cutoff,
        "recover_final_from_phase_zero": bool(recovery),
        "recovered_count": len(recovery.recovered) if recovery else 0,
        "input_sha256": {
            "q2frame": _sha(args.q2frame), "td_rows": _sha(args.td_rows),
            "plate_snapshot": _sha(args.plate_snapshot), "pool": _sha(args.pool),
        },
        "read_only": "local files only; no Redis/TD/Rabbit",
    }
    (args.out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": report.status, "components": dict(report.component_status),
                      "content_hash": report.content_hash}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
