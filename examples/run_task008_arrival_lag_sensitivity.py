"""Measure auction-anchor sensitivity to explicitly hypothetical read lag.

Input is one hash-pinned real t1-v2 Q2Frame JSONL capture. Every source frame
and update from the beginning through the 09:25:30 source-time prefix is read
in order; this runner does not sample symbols, connect to external services,
or infer Rabbit arrival/historical availability. It models the Redis-like
latest-per-symbol state at periodic read times and evaluates existing Core
auction-anchor facts at their configured barriers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterator, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core.auction_timeline import (  # noqa: E402
    AuctionTimingPolicyV1,
    build_auction_anchor_revision,
)
from engine_core.clock import local_datetime_ms  # noqa: E402
from engine_core.contracts import canonical_hash, semantic_hash  # noqa: E402
from engine_core.q2 import normalize_q2, normalize_symbol  # noqa: E402
from engine_core.replay import Q2FrameV1  # noqa: E402
from engine_core.replay_fidelity import replay_fidelity_metadata_for_input_kind  # noqa: E402
from engine_core.replay_arrival import (  # noqa: E402
    ArrivalLagModelV1,
    iter_hypothetical_reads,
)


DEFAULT_INPUT = Path(
    "/home/exedev/validation/task009-f-t1-prefix-q2frame-vs-capture-20261011/run/prefix.q2frame.jsonl"
)
DEFAULT_SHA256 = "7474de28bfc60e8777cef0f359b4f6d2ab1b4f1ab6bcdf4fee5211a721483467"
DEFAULT_TRADE_DATE = "2026-10-09"
LAGS_MS = (0, 3_000, 6_000, 10_000, 30_000)
CADENCES_MS = (1_000, 3_000)
ANCHOR_TAGS = ("0920", "0924", "0925")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _raw_frames(path: Path) -> Iterator[Mapping[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            raw = json.loads(line)
            if not isinstance(raw, Mapping):
                raise ValueError("Q2Frame line %d is not an object" % line_no)
            yield raw


def _inventory(path: Path) -> dict[str, Any]:
    frames = updates = empty_frames = 0
    symbols: set[str] = set()
    first_logical_ms = last_logical_ms = None
    for raw in _raw_frames(path):
        frame = Q2FrameV1.from_mapping(raw)
        frames += 1
        updates += len(frame.q2_updates)
        empty_frames += not bool(frame.q2_updates)
        symbols.update(str(update["symbol"]) for update in frame.q2_updates)
        if first_logical_ms is None:
            first_logical_ms = frame.logical_ts_ms
        if last_logical_ms is not None and frame.logical_ts_ms < last_logical_ms:
            raise ValueError("input Q2Frame logical time moved backwards")
        last_logical_ms = frame.logical_ts_ms
    if not frames:
        raise ValueError("input Q2Frame artifact is empty")
    return {
        "frame_count": frames,
        "update_count": updates,
        "empty_frame_count": empty_frames,
        "symbol_count": len(symbols),
        "symbols": tuple(sorted(symbols)),
        "first_logical_ts_ms": first_logical_ms,
        "last_logical_ts_ms": last_logical_ms,
    }


def _prefix_frames(path: Path, source_end_ms: int) -> Iterator[Q2FrameV1]:
    last_seq = None
    last_logical_ms = None
    for raw in _raw_frames(path):
        frame = Q2FrameV1.from_mapping(raw)
        if last_seq is not None and frame.seq_no != last_seq + 1:
            raise ValueError("input Q2Frame seq_no is not continuous")
        if last_logical_ms is not None and frame.logical_ts_ms < last_logical_ms:
            raise ValueError("input Q2Frame logical time moved backwards")
        last_seq = frame.seq_no
        last_logical_ms = frame.logical_ts_ms
        if frame.logical_ts_ms > source_end_ms:
            break
        yield frame


def _milestones(trade_date: str, lag_ms: int) -> tuple[dict[int, dict[str, list[str]]], tuple[int, ...]]:
    roles: dict[int, dict[str, list[str]]] = {}
    times: set[int] = set()
    for tag in ANCHOR_TAGS:
        policy = AuctionTimingPolicyV1.default(tag)
        policy_times = policy.at(trade_date)
        for key, role in (
            ("first_observable_ms", "first_observable"),
            ("preferred_finalize_ms", "preferred_finalize"),
            ("soft_deadline_ms", "soft_deadline"),
        ):
            timestamp = int(policy_times[key])
            roles.setdefault(timestamp, {}).setdefault(tag, []).append(role)
            times.add(timestamp)
        convergence = int(policy_times["soft_deadline_ms"]) + lag_ms
        roles.setdefault(convergence, {}).setdefault(tag, []).append(
            "convergence_after_soft_plus_assumed_lag"
        )
        times.add(convergence)
    return roles, tuple(sorted(times))


def _anchor_snapshot(
    *,
    trade_date: str,
    tag: str,
    read_time_ms: int,
    raw_by_symbol: Mapping[str, Mapping[str, Any]],
    expected_symbols: tuple[str, ...],
) -> dict[str, Any]:
    rows: dict[str, Mapping[str, Any]] = {}
    quality_counts: dict[str, int] = {}
    for symbol, raw in raw_by_symbol.items():
        quote = normalize_q2(symbol, raw).to_mapping()
        rows[symbol] = quote
        quality = quote.get("auction_anchor_field_quality", {}).get("a%s" % tag)
        quality_counts[str(quality)] = quality_counts.get(str(quality), 0) + 1
    revision = build_auction_anchor_revision(
        trade_date,
        tag,
        rows,
        evaluation_time_ms=read_time_ms,
        observed_at_ms=read_time_ms,
        expected_symbols=expected_symbols,
        source_layers=("HYPOTHETICAL_Q2FRAME_READ",),
    )
    return {
        "tag": tag,
        "modeled_read_time_ms": read_time_ms,
        "state": revision.state,
        "expected_symbols_from_full_capture": len(expected_symbols),
        "source_observed_count": len(revision.source_observed_symbols),
        "source_missing_count": len(revision.source_missing_symbols),
        "available_count": len(revision.available_anchor_symbols),
        "missing_anchor_count": len(revision.missing_anchor_symbols),
        "coverage_within_capture_universe": revision.anchor_coverage,
        "available_symbols_hash": semantic_hash(revision.available_anchor_symbols),
        "anchor_content_hash": revision.content_hash,
        "source_time_min_ms": revision.source_time_min_ms,
        "source_time_max_ms": revision.source_time_max_ms,
        "late_execution_under_core_policy": revision.late_execution,
        "field_quality_counts_among_observed_rows": quality_counts,
    }


def _run_scenario(
    *,
    path: Path,
    trade_date: str,
    expected_symbols: tuple[str, ...],
    source_end_ms: int,
    lag_ms: int,
    cadence_ms: int,
) -> dict[str, Any]:
    model = ArrivalLagModelV1.none() if lag_ms == 0 else ArrivalLagModelV1.constant(lag_ms)
    role_at_time, evaluation_times = _milestones(trade_date, lag_ms)
    diagnostics: dict[str, Any] = {}
    raw_by_symbol: dict[str, dict[str, Any]] = {}
    snapshots: list[dict[str, Any]] = []
    read_count = market_frame_count = 0
    last_source_logical_ms = None
    for read in iter_hypothetical_reads(
        _prefix_frames(path, source_end_ms),
        model,
        cadence_ms=cadence_ms,
        evaluation_times_ms=evaluation_times,
        diagnostics=diagnostics,
    ):
        read_count += 1
        if read.frame is not None:
            market_frame_count += 1
            for update in read.frame.q2_updates:
                symbol = normalize_symbol(update["symbol"])
                state = raw_by_symbol.setdefault(symbol, {})
                state.update({key: value for key, value in update.items() if key != "symbol"})
                try:
                    last_source_logical_ms = max(last_source_logical_ms or 0, int(update.get("ts", 0)))
                except (TypeError, ValueError, OverflowError):
                    pass
        if read.read_time_ms in role_at_time:
            by_tag: dict[str, Any] = {}
            for tag, roles in sorted(role_at_time[read.read_time_ms].items()):
                by_tag[tag] = {
                    "roles": tuple(roles),
                    "snapshot": _anchor_snapshot(
                        trade_date=trade_date,
                        tag=tag,
                        read_time_ms=read.read_time_ms,
                        raw_by_symbol=raw_by_symbol,
                        expected_symbols=expected_symbols,
                    ),
                }
            snapshots.append({
                "read_time_ms": read.read_time_ms,
                "source_frame_seq_nos": read.source_frame_seq_nos,
                "new_update_count": len(read.frame.q2_updates) if read.frame else 0,
                "max_assumed_arrival_ms": read.max_arrival_ms,
                "anchors": by_tag,
            })
    return {
        "model": dict(model.metadata),
        "assumed_constant_lag_ms": lag_ms,
        "read_cadence_ms": cadence_ms,
        "source_prefix_end_ms_inclusive": source_end_ms,
        "source_frames_processed": diagnostics.get("input_frame_count", 0),
        "source_updates_processed": diagnostics.get("input_update_count", 0),
        "source_empty_frames_processed": diagnostics.get("input_empty_frame_count", 0),
        "read_poll_count": read_count,
        "market_input_frame_count": market_frame_count,
        "source_timestamp_regressions": diagnostics.get("source_ts_regression_count", 0),
        "source_timestamps_after_q2frame_logical_time": diagnostics.get(
            "source_ts_after_frame_logical_count", 0
        ),
        "invalid_source_timestamps": diagnostics.get("source_ts_invalid_count", 0),
        "input_identity_matches_delivered": diagnostics.get("input_identity_matches_delivered"),
        "input_update_multiset_fingerprint": diagnostics.get("input_update_multiset_fingerprint"),
        "delivered_update_multiset_fingerprint": diagnostics.get("delivered_update_multiset_fingerprint"),
        "last_delivered_source_timestamp_ms": last_source_logical_ms,
        "slot_convention": diagnostics.get("slot_convention"),
        "lookahead_max_ms": diagnostics.get("lookahead_max_ms"),
        "source_sequence_status": diagnostics.get("source_sequence_status"),
        "snapshots": snapshots,
    }


def _compare_zero_lag_cadences(results: list[dict[str, Any]]) -> dict[str, Any]:
    baselines = [
        item for item in results
        if item["assumed_constant_lag_ms"] == 0
    ]
    if len(baselines) < 2:
        return {"status": "NOT_COMPARABLE", "reason": "need at least two zero-lag cadences"}
    by_cadence = {item["read_cadence_ms"]: item for item in baselines}
    reference = by_cadence[min(by_cadence)]
    comparisons = []
    for candidate in baselines:
        reference_snapshots = {
            (snapshot["read_time_ms"], tag): entry["snapshot"]["anchor_content_hash"]
            for snapshot in reference["snapshots"]
            for tag, entry in snapshot["anchors"].items()
        }
        candidate_snapshots = {
            (snapshot["read_time_ms"], tag): entry["snapshot"]["anchor_content_hash"]
            for snapshot in candidate["snapshots"]
            for tag, entry in snapshot["anchors"].items()
        }
        comparisons.append({
            "reference_cadence_ms": reference["read_cadence_ms"],
            "candidate_cadence_ms": candidate["read_cadence_ms"],
            "snapshot_anchor_keys_match": set(reference_snapshots) == set(candidate_snapshots),
            "anchor_content_hashes_match": reference_snapshots == candidate_snapshots,
        })
    equal = all(item["anchor_content_hashes_match"] for item in comparisons)
    return {
        "status": "MATCH" if equal else "DIFFERENT",
        "comparisons": comparisons,
        "scope": "same source prefix and exact milestone cutoffs; no Rabbit arrival claim",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--expected-sha256", default=DEFAULT_SHA256)
    parser.add_argument("--trade-date", default=DEFAULT_TRADE_DATE)
    parser.add_argument(
        "--input-kind",
        help=(
            "explicit input provenance label; TD fidelity diagnostics are attached "
            "only for TD_RETAINED_EVENT_REPLAY"
        ),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--lags-ms", type=int, nargs="+", default=LAGS_MS)
    parser.add_argument("--cadences-ms", type=int, nargs="+", default=CADENCES_MS)
    args = parser.parse_args()

    if not args.input.is_file():
        raise SystemExit("input Q2Frame file does not exist")
    input_sha = _sha256(args.input)
    if input_sha != args.expected_sha256:
        raise SystemExit("input SHA-256 did not match the pinned value")
    inventory = _inventory(args.input)
    expected_symbols = tuple(inventory["symbols"])
    source_end_ms = local_datetime_ms(args.trade_date, "09:25:30")
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)

    results = []
    for lag_ms in args.lags_ms:
        if lag_ms < 0:
            raise SystemExit("lag values must be non-negative")
        for cadence_ms in args.cadences_ms:
            if cadence_ms <= 0 or cadence_ms % 1_000:
                raise SystemExit("cadences must be positive whole-second multiples")
            result = _run_scenario(
                path=args.input,
                trade_date=args.trade_date,
                expected_symbols=expected_symbols,
                source_end_ms=source_end_ms,
                lag_ms=lag_ms,
                cadence_ms=cadence_ms,
            )
            results.append(result)
            print(
                "scenario complete: lag=%dms cadence=%dms source_frames=%d updates=%d "
                "identity=%s" % (
                    lag_ms,
                    cadence_ms,
                    result["source_frames_processed"],
                    result["source_updates_processed"],
                    result["input_identity_matches_delivered"],
                ),
                flush=True,
            )

    replay_fidelity = replay_fidelity_metadata_for_input_kind(
        args.input_kind,
        run_trade_date=args.trade_date,
        run_input_sha256=input_sha,
    )
    summary = {
        "contract": (
            "ArrivalLagSensitivityRunV2"
            if replay_fidelity is not None
            else "ArrivalLagSensitivityRunV1"
        ),
        "run_mode": "HYPOTHETICAL_ARRIVAL_LAG_SENSITIVITY",
        "result_status": "DIAGNOSTIC_ONLY",
        "trade_date": args.trade_date,
        **({"input_kind": args.input_kind} if args.input_kind is not None else {}),
        "input": {
            "path": str(args.input),
            "sha256": input_sha,
            "full_capture_inventory": {
                key: value for key, value in inventory.items() if key != "symbols"
            },
            "source_prefix_start_ms": inventory["first_logical_ts_ms"],
            "source_prefix_end_ms_inclusive": source_end_ms,
            "prefix_selection_reason": "all captured frames through latest auction soft deadline; no symbol sampling",
            "expected_universe": "all symbols observed anywhere in pinned Q2Frame capture; not a full-market claim",
            "expected_symbol_count": len(expected_symbols),
        },
        "arrival_order_status": "UNKNOWN",
        "historical_available_at_status": "UNKNOWN_NOT_INFERRED",
        "lookahead_max_ms": 0,
        "source_update_timestamps_modified": False,
        "read_slot_convention": "CAUSAL_RIGHT_CLOSED",
        "cadence_comparison": _compare_zero_lag_cadences(results),
        "scenarios": results,
        "non_claims": [
            "not Rabbit arrival or queue membership",
            "not historical available_at",
            "not a live replay or NORMAL parity result",
            "not full-market completeness",
            "not a claim that constant lag values are empirically realistic",
        ],
    }
    if replay_fidelity is not None:
        summary["replay_fidelity"] = replay_fidelity
    summary_path = output_dir / "arrival_lag_sensitivity.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report_path = output_dir / "report.md"
    report_path.write_text(_render_report(summary), encoding="utf-8")
    checksums = []
    for path in sorted(output_dir.iterdir()):
        if path.is_file():
            checksums.append("%s  %s" % (_sha256(path), path.name))
    (output_dir / "sha256sums.txt").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    print("wrote %s" % output_dir, flush=True)
    return 0


def _render_report(summary: Mapping[str, Any]) -> str:
    lines = [
        "# Hypothetical arrival-lag sensitivity",
        "",
        "Status: `DIAGNOSTIC_ONLY` — this is not Rabbit arrival evidence.",
        "",
        "- Trade date: `%s`" % summary["trade_date"],
        "- Input SHA-256: `%s`" % summary["input"]["sha256"],
        "- Prefix: all captured frames through 09:25:30; no symbol sampling.",
        "- Expected denominator: all symbols in the frozen Q2Frame capture (%d); not full market." % summary["input"]["expected_symbol_count"],
        "- Rabbit arrival: `UNKNOWN`; historical `available_at`: `UNKNOWN_NOT_INFERRED`.",
        "- Zero-lag 1s/3s cadence comparison: `%s`." % summary["cadence_comparison"]["status"],
        "- Scope: local latest-per-symbol merge plus the Core anchor-revision builder only; no Q2FrameReplaySource, Engine, AuctionTimeline revision chain, or AuctionShadowStrategy.",
        "",
        "For constant lag `L`, a read at `R` queries this captured availability curve at `R-L`; the lag values are not independent empirical distributions. The zero-lag cadence comparison is a schedule self-check at exact milestone reads, not a market-data finding.",
        "",
        "| Assumed lag | Poll cadence | Input frames | Updates | Conservation | 0925 milestones (available / denominator) |",
        "|---:|---:|---:|---:|---|---|",
    ]
    for scenario in summary["scenarios"]:
        anchor_counts = []
        for snapshot in scenario["snapshots"]:
            entry = snapshot["anchors"].get("0925")
            if entry:
                roles = "+".join(entry["roles"])
                value = entry["snapshot"]
                anchor_counts.append("%s %d/%d" % (
                    roles,
                    value["available_count"],
                    value["expected_symbols_from_full_capture"],
                ))
        lines.append(
            "| %d ms | %d ms | %d | %d | %s | %s |" % (
                scenario["assumed_constant_lag_ms"],
                scenario["read_cadence_ms"],
                scenario["source_frames_processed"],
                scenario["source_updates_processed"],
                "PASS" if scenario["input_identity_matches_delivered"] else "CHECK",
                "; ".join(anchor_counts),
            )
        )
    lines.extend([
        "",
        "The model sets assumed arrival time to `Q2Frame.logical_ts_ms + constant_lag`; source `ts` values are unchanged. At each causal right-closed poll, all newly arrived source updates are applied in source-frame/update order. Empty polling slots do not become market frames; actual empty source frames are preserved.",
        "",
        "This measures sensitivity of Core anchor facts to assumed visibility delay and read cadence. It does not establish actual queue order, delivery, wall-clock visibility, historical availability, or NORMAL equivalence.",
        "",
    ])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
