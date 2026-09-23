"""Replay a t1-v2 Q2FrameV1 artifact through Core without recomputing Q2.

The producer invocation is intentionally separate and should use t1-v2's
``--replay --dry-run --q2frame`` flags.  This runner only consumes the local
JSONL artifact.  It never imports Redis/TD/Rabbit clients and never writes an
external system.  Q2Frame source timestamps are treated as data; no market
hours gate is applied here.
Historical ``available_at`` is not inferred from Q2Frame logical/source time
or from the replay clock.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core import (  # noqa: E402
    DeterministicEngine,
    MarketStateReducer,
    ProbeStrategy,
    Q2FrameReplaySource,
    Q2FrameV1,
    VirtualClock,
    WindowManager,
    WindowSpec,
    canonical_hash,
)
from engine_core.q2 import normalize_symbol  # noqa: E402


def _open_jsonl(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def _iter_raw(path: Path) -> Iterator[Mapping[str, Any]]:
    with _open_jsonl(path) as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise ValueError(f"Q2Frame line {line_no} is not an object")
            yield value


def _inventory(path: Path) -> dict[str, Any]:
    frames = updates = 0
    symbols: set[str] = set()
    first_ts = last_ts = None
    for raw in _iter_raw(path):
        frame = Q2FrameV1.from_mapping(raw)
        frames += 1
        updates += len(frame.q2_updates)
        symbols.update(str(item["symbol"]) for item in frame.q2_updates)
        if first_ts is None:
            first_ts = frame.logical_ts_ms
        if last_ts is not None and frame.logical_ts_ms < last_ts:
            raise ValueError("Q2Frame logical timestamps moved backwards")
        last_ts = frame.logical_ts_ms
    if frames == 0:
        raise ValueError("Q2Frame artifact is empty")
    return {
        "frame_count": frames,
        "update_count": updates,
        "symbol_count": len(symbols),
        "symbols": tuple(sorted(symbols)),
        "first_logical_ts_ms": first_ts,
        "last_logical_ts_ms": last_ts,
    }


def _engine() -> DeterministicEngine:
    return DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("q2_replay", 0, 10**16),)),
        ProbeStrategy(),
        session_id="TASK-008-Q2FRAME",
        phase="REPLAY",
    )


def _run_once(
    path: Path,
    trade_date: str,
    expected_symbols: tuple[str, ...],
    inventory: Mapping[str, Any],
    *,
    coverage_basis: str,
) -> dict[str, Any]:
    initial = datetime.fromtimestamp(inventory["first_logical_ts_ms"] / 1000, timezone.utc)
    clock = VirtualClock(initial)
    source = Q2FrameReplaySource(
        trade_date,
        expected_symbols,
        clock,
        source_id="t1_v2_q2frame_replay",
    )
    engine = _engine()
    frame_hashes: list[str] = []
    projection_hashes: list[str] = []
    per_frame_coverage: list[dict[str, Any]] = []
    expected = set(expected_symbols)
    for raw in _iter_raw(path):
        frame = Q2FrameV1.from_mapping(raw)
        signal = source.signal_for(frame, signal_prefix="t1-v2-q2frame")
        source.advance_before_consume(signal)
        engine.submit(signal)
        result = engine.run_until_empty()
        # EngineRunResult.processed_signals is cumulative for this Engine.
        processed_signals = result.processed_signals
        frame_hashes.append(canonical_hash(raw))
        projection_hashes.append(signal.payload.content_hash)
        frame_symbols = {str(item["symbol"]) for item in frame.q2_updates}
        frame_covered = frame_symbols & expected
        projection_symbols = set(signal.payload.quotes)
        per_frame_coverage.append({
            "seq_no": frame.seq_no,
            "updated_symbol_count": len(frame_covered),
            "updated_coverage": len(frame_covered) / len(expected) if expected else 0.0,
            "projection_observed_symbol_count": len(projection_symbols),
            "projection_coverage": signal.payload.coverage,
            "projection_missing_symbols": signal.payload.missing_symbols,
        })
    state = engine._reducer.state  # evidence-only read of the in-memory reducer
    final_state_hash = canonical_hash(
        {
            "revision": state.revision,
            "logical_time_ms": state.logical_time_ms,
            "coverage": state.coverage,
            "completeness": state.completeness,
            "symbols": state.symbol_states,
            "source": state.source_observation_metadata,
        }
    )
    covered_symbols = set(inventory["symbols"]) & expected
    virtual_clock = {
        "final_utc": clock.now_utc().isoformat(),
        "final_epoch_ms": int(clock.now_utc().timestamp() * 1000),
        "final_monotonic_ns": clock.now_ns(),
    }
    return {
        "frame_hashes": tuple(frame_hashes),
        "projection_hashes": tuple(projection_hashes),
        "processed_signals": processed_signals,
        "reducer_revision": state.revision,
        "virtual_clock": virtual_clock,
        "final_state_hash": final_state_hash,
        "symbol_coverage": {
            "basis": coverage_basis,
            "expected_symbol_count": len(expected_symbols),
            "expected_symbols": tuple(expected_symbols),
            "q2frame_symbol_count": len(inventory["symbols"]),
            "q2frame_symbols": tuple(inventory["symbols"]),
            "covered_symbol_count": len(covered_symbols),
            "covered_symbols": tuple(sorted(covered_symbols)),
            "missing_expected_symbols": tuple(sorted(expected - covered_symbols)),
            "out_of_scope_q2_symbols": tuple(sorted(set(inventory["symbols"]) - expected)),
            "coverage": len(covered_symbols) / len(expected) if expected else 0.0,
            "per_frame": tuple(per_frame_coverage),
        },
    }


_DETERMINISM_FIELDS = (
    "frame_hashes",
    "projection_hashes",
    "processed_signals",
    "reducer_revision",
    "final_state_hash",
    "virtual_clock",
    "symbol_coverage",
)


def _compare_runs(
    first: Mapping[str, Any],
    repeat: Mapping[str, Any],
    *,
    input_sha256_stable: bool,
) -> dict[str, bool]:
    return {
        **{field: first[field] == repeat[field] for field in _DETERMINISM_FIELDS},
        "input_sha256_stable": input_sha256_stable,
    }


def _replay_status(comparison: Mapping[str, bool]) -> str:
    return "REPLAY_READY_BOUNDED" if all(comparison.values()) else "REPLAY_NON_DETERMINISTIC"


def _read_tick_evidence(path: Path, trade_date: str) -> tuple[dict[str, Any], tuple[str, ...]]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping):
        raise ValueError("tick evidence manifest must be an object")
    if manifest.get("trade_date") != trade_date:
        raise ValueError("tick evidence trade_date does not match Q2 replay")
    raw_symbols = manifest.get("expected_symbols", ())
    if not isinstance(raw_symbols, (list, tuple)):
        raise ValueError("tick evidence expected_symbols must be a list")
    expected_symbols = tuple(sorted({normalize_symbol(item) for item in raw_symbols}))
    if not expected_symbols:
        raise ValueError("tick evidence expected_symbols must not be empty")
    return (
        {
            "path": str(path),
            "sha256": _sha256(path),
            "manifest": manifest,
        },
        expected_symbols,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--q2frame", type=Path, required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tick-evidence", type=Path)
    args = parser.parse_args()

    if args.output.resolve() == args.q2frame.resolve():
        parser.error("--output must not overwrite --q2frame")
    if args.tick_evidence is not None and args.output.resolve() == args.tick_evidence.resolve():
        parser.error("--output must not overwrite --tick-evidence")
    input_sha256_initial = _sha256(args.q2frame)
    inventory = _inventory(args.q2frame)
    input_sha256_after_inventory = _sha256(args.q2frame)
    tick_evidence = None
    if args.tick_evidence is None:
        expected_symbols = tuple(inventory["symbols"])
        coverage_basis = "Q2FRAME_UNIQUE_SYMBOLS_ONLY"
    else:
        tick_evidence, expected_symbols = _read_tick_evidence(args.tick_evidence, args.trade_date)
        coverage_basis = "TICK_EVIDENCE_EXPECTED_SYMBOLS"

    input_sha256_before = input_sha256_after_inventory
    first = _run_once(
        args.q2frame,
        args.trade_date,
        expected_symbols,
        inventory,
        coverage_basis=coverage_basis,
    )
    input_sha256_between = _sha256(args.q2frame)
    repeat = _run_once(
        args.q2frame,
        args.trade_date,
        expected_symbols,
        inventory,
        coverage_basis=coverage_basis,
    )
    input_sha256_after = _sha256(args.q2frame)
    input_sha256_stable = len({
        input_sha256_initial,
        input_sha256_after_inventory,
        input_sha256_between,
        input_sha256_after,
    }) == 1
    comparison = _compare_runs(first, repeat, input_sha256_stable=input_sha256_stable)
    status = _replay_status(comparison)

    tick_coverage = None
    if args.tick_evidence is not None:
        q2_symbols = set(inventory["symbols"])
        missing_q2_symbols = sorted(set(expected_symbols) - q2_symbols)
        tick_coverage = {
            "expected_count": len(expected_symbols),
            "observed_q2_symbol_count": len(set(expected_symbols) & q2_symbols),
            "missing_q2_symbol_count": len(missing_q2_symbols),
            "missing_q2_symbols_sample": missing_q2_symbols[:20],
            "coverage": (
                len(set(expected_symbols) & q2_symbols) / len(expected_symbols)
                if expected_symbols else 0.0
            ),
        }

    report = {
        "contract_version": "Task008T1V2Q2FrameReplayV2",
        "trade_date": args.trade_date,
        "q2frame": {
            "path": str(args.q2frame),
            "sha256_before_inventory": input_sha256_initial,
            "sha256_before_first_run": input_sha256_before,
            "sha256_between_runs": input_sha256_between,
            "sha256_after": input_sha256_after,
        },
        "inventory": inventory,
        "ordered": first,
        "repeat": repeat,
        "determinism": comparison,
        "deterministic": all(comparison.values()),
        "tick_evidence": tick_evidence,
        "tick_q2_coverage": tick_coverage,
        "same_input_provenance": "Q2FRAME_FILE_SHA256_CHECKED_BEFORE_INVENTORY_BEFORE_BETWEEN_AND_AFTER_RUNS",
        "q2_source": "t1-v2",
        "q2_time_policy": "SOURCE_TIME_ONLY; NO_MARKET_HOURS_GATE",
        "historical_available_at": "UNKNOWN_NOT_INFERRED",
        "historical_available_at_policy": "NOT_INFERRED_FROM_LOGICAL_TS_SOURCE_TS_OR_REPLAY_TIME",
        "production_side_effects": "NONE_OBSERVED",
        "replay_status": status,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "frames": inventory["frame_count"],
        "updates": inventory["update_count"],
        "symbols": inventory["symbol_count"],
        "deterministic": report["deterministic"],
        "replay_status": status,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if status == "REPLAY_READY_BOUNDED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
