#!/usr/bin/env python3
"""Compare Core's Q2-derived auction summary with frozen real A2 evidence.

This runner consumes a recorded Q2Frame JSONL stream one frame at a time and
frozen Redis captures supplied by the caller. It performs no Redis, TDengine,
RabbitMQ, or production-service I/O.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from engine_core import (
    derive_q2_auction_summary,
    normalize_auction_market_summary,
    normalize_q2,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
VALIDATION_ROOT = Path("/home/exedev/validation").resolve()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True, help="strict YYYY-MM-DD")
    parser.add_argument("--cutoff-ts-ms", required=True, type=int)
    parser.add_argument("--q2frame", required=True, type=Path)
    parser.add_argument("--a2-anchor", required=True, type=Path)
    parser.add_argument("--a2-summary", required=True, type=Path)
    parser.add_argument("--producer-release", required=True)
    parser.add_argument("--producer-binary-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    output_path = args.output.resolve()
    if not output_path.is_relative_to(VALIDATION_ROOT):
        raise SystemExit("--output must be under /home/exedev/validation")
    if output_path.exists():
        raise SystemExit("refusing to overwrite existing validation evidence")

    q2_latest: dict[str, dict[str, Any]] = {}
    frame_count = 0
    accepted_update_count = 0
    last_frame_ts_ms: int | None = None
    cutoff_datetime = datetime.fromtimestamp(args.cutoff_ts_ms / 1000, tz=SHANGHAI)
    with args.q2frame.open(encoding="utf-8") as stream:
        for line in stream:
            frame = json.loads(line)
            frame_ts_ms = int(frame["logical_ts_ms"])
            if frame_ts_ms > args.cutoff_ts_ms:
                continue
            frame_count += 1
            last_frame_ts_ms = frame_ts_ms
            for row in frame["q2_updates"]:
                symbol = str(row["symbol"])
                q2_latest[symbol] = row
                accepted_update_count += 1

    if not q2_latest:
        raise SystemExit("no Q2 updates were found at or before the requested cutoff")

    q2_quotes = {
        symbol: normalize_q2(symbol, raw)
        for symbol, raw in q2_latest.items()
    }
    q2frame_sha256 = _sha256_file(args.q2frame)
    anchor_sha256 = _sha256_file(args.a2_anchor)
    summary_sha256 = _sha256_file(args.a2_summary)

    anchor_payload = json.loads(args.a2_anchor.read_text(encoding="utf-8"))
    anchor_symbols = set(anchor_payload)
    summary_capture = json.loads(args.a2_summary.read_text(encoding="utf-8"))
    summary_source = summary_capture["source_capture"]
    live_summary = normalize_auction_market_summary(
        summary_capture["summary"],
        trade_date=args.trade_date,
        source_id=summary_source["source_id"],
        source_table=summary_source["source_table"],
        observation_time_ms=summary_source.get("observation_time_ms"),
        evidence_refs=(summary_source["capture_sha256"],),
    )
    derived = derive_q2_auction_summary(
        q2_quotes,
        trade_date=args.trade_date,
        source_id="q2frame-sha256:" + q2frame_sha256,
        source_table="Q2FrameV1:event_time_replay",
        expected_symbols=tuple(q2_quotes),
        # Q2 historical available_at is not present in this capture.
        observation_time_ms=None,
        input_content_hash=q2frame_sha256,
        evidence_refs=(q2frame_sha256, anchor_sha256, summary_sha256),
    )

    live_values = {
        "stock_count": live_summary.stock_count,
        "valid_stock_count": live_summary.valid_stock_count,
        "unavailable_stock_count": live_summary.unavailable_stock_count,
        "positive_count": live_summary.positive_count,
        "negative_count": live_summary.negative_count,
        "flat_count": live_summary.flat_count,
        "auction_amount_yuan": live_summary.auction_amount_yuan,
        "limit_up_count": live_summary.limit_up_count,
        "limit_down_count": live_summary.limit_down_count,
        "limit_up_seal_amount_yuan": live_summary.limit_up_seal_amount_yuan,
    }
    derived_values = dict(derived.metrics)
    candidate_set_match = set(derived.candidate_symbols) == anchor_symbols
    summary_fields_match = derived_values == live_values
    summary_snapshot_ts_ms = summary_capture["summary"].get("ts")
    summary_snapshot_delta_ms = (
        int(summary_snapshot_ts_ms) - args.cutoff_ts_ms
        if summary_snapshot_ts_ms is not None
        else None
    )
    bounded_checks = {
        "projection_ready": derived.status.value == "READY",
        "no_unknown_candidate_membership": derived.candidate_membership_unknown_count == 0,
        "no_unknown_summary_metrics": not any(derived.metric_unknown_counts.values()),
        "candidate_set_matches_frozen_a2_anchor": candidate_set_match,
        "all_ten_summary_fields_match": summary_fields_match,
    }
    report = {
        "schema": "Task008Q2A2RealDerivationV1",
        "trade_date": args.trade_date,
        "cutoff": {
            "logical_ts_ms": args.cutoff_ts_ms,
            "local_time": cutoff_datetime.isoformat(),
            "last_included_q2frame_ts_ms": last_frame_ts_ms,
            "included_frame_count": frame_count,
            "included_q2_update_count": accepted_update_count,
        },
        "producer": {
            "release": args.producer_release,
            "binary_sha256": args.producer_binary_sha256,
        },
        "inputs": {
            "q2frame": {"path": str(args.q2frame), "sha256": q2frame_sha256},
            "a2_anchor": {
                "path": str(args.a2_anchor),
                "sha256": anchor_sha256,
                "observed_symbols": len(anchor_symbols),
            },
            "a2_summary": {
                "path": str(args.a2_summary),
                "sha256": summary_sha256,
                "snapshot_ts_ms": summary_capture["summary"].get("ts"),
                "observation_time_ms": summary_source.get("observation_time_ms"),
            },
        },
        "comparison_clock": {
            "q2_cutoff_ts_ms": args.cutoff_ts_ms,
            "a2_summary_snapshot_ts_ms": summary_snapshot_ts_ms,
            "a2_summary_snapshot_delta_from_q2_cutoff_ms": summary_snapshot_delta_ms,
            "interpretation": "bounded cross-capture parity; not a same-instant comparison",
        },
        "result": {
            "status": derived.status.value,
            "input_symbols": derived.input_symbol_count,
            "candidate_symbols": derived.candidate_count,
            "candidate_membership_unknown": derived.candidate_membership_unknown_count,
            "candidate_set_exact_match_to_frozen_a2_anchor": candidate_set_match,
            "derived_summary_values": derived_values,
            "frozen_a2_summary_values": live_values,
            "all_ten_summary_fields_match": summary_fields_match,
            "metric_unknown_counts": dict(derived.metric_unknown_counts),
            "invalid_field_count": len(derived.invalid_fields),
            "q2_input_coverage_within_observed_set": derived.q2_input_coverage,
            "market_universe_coverage_status": derived.market_universe_coverage_status,
            "candidate_rule": derived.candidate_rule,
            "candidate_rule_status": derived.candidate_rule_status,
            "core_content_hash": derived.content_hash,
            "core_evidence_hash": derived.evidence_hash,
            "bounded_result_checks": bounded_checks,
            "bounded_result": (
                "PASS_BOUNDED"
                if all(bounded_checks.values())
                else "MISMATCH"
            ),
        },
        "limits": [
            "Q2Frame event-time order is not Rabbit arrival order.",
            "historical Q2 available_at is UNKNOWN.",
            (
                "Q2 does not expose t1-v2 internal auction.ts_ms; candidate "
                "membership is derived from non-zero am/br/ar and only this "
                "captured member set is compared with Redis A2."
            ),
            "The observed 5,220-symbol Q2 cohort is not asserted to be the full-market universe.",
            (
                "A2 equality is same-release pipeline parity, not an independent "
                "market-truth or strategy oracle."
            ),
        ],
        "side_effects": {
            "live_source_connections": 0,
            "redis_writes": 0,
            "td_writes": 0,
            "rabbit_access": 0,
            "service_changes": 0,
            "classification": "NONE_OBSERVED",
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["result"], ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["result"]["bounded_result"] == "PASS_BOUNDED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
