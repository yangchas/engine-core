"""Replay a t1-v2 Q2FrameV1 artifact through Core without recomputing Q2.

The producer invocation is intentionally separate and should use t1-v2's
``--replay --dry-run --q2frame`` flags.  This runner only consumes the local
JSONL artifact.  It never imports Redis/TD/Rabbit clients and never writes an
external system.  Q2Frame source timestamps are treated as data; no market
hours gate is applied here.
"""

from __future__ import annotations

import argparse
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


def _iter_raw(path: Path) -> Iterator[Mapping[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
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
    symbols: tuple[str, ...],
    inventory: Mapping[str, Any],
) -> dict[str, Any]:
    initial = datetime.fromtimestamp(inventory["first_logical_ts_ms"] / 1000, timezone.utc)
    source = Q2FrameReplaySource(
        trade_date,
        symbols,
        VirtualClock(initial),
        source_id="t1_v2_q2frame_replay",
    )
    engine = _engine()
    frame_hashes: list[dict[str, Any]] = []
    processed_signals = 0
    for raw in _iter_raw(path):
        frame = Q2FrameV1.from_mapping(raw)
        signal = source.signal_for(frame, signal_prefix="t1-v2-q2frame")
        source.advance_before_consume(signal)
        engine.submit(signal)
        result = engine.run_until_empty()
        processed_signals += result.processed_signals
        frame_hashes.append(
            {
                "seq_no": frame.seq_no,
                "logical_ts_ms": frame.logical_ts_ms,
                "update_count": len(frame.q2_updates),
                "projection_hash": signal.payload.content_hash,
            }
        )
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
    return {
        "inventory": inventory,
        "processed_signals": processed_signals,
        "reducer_revision": state.revision,
        "final_virtual_clock_ms": int(source._clock.now_utc().timestamp() * 1000),
        "final_projection_hash": frame_hashes[-1]["projection_hash"],
        "final_state_hash": final_state_hash,
        "frame_hashes_hash": canonical_hash(frame_hashes),
    }


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

    inventory = _inventory(args.q2frame)
    symbols = tuple(inventory["symbols"])
    first = _run_once(args.q2frame, args.trade_date, symbols, inventory)
    second = _run_once(args.q2frame, args.trade_date, symbols, inventory)
    comparison = {
        key: first[key] == second[key]
        for key in (
            "processed_signals",
            "reducer_revision",
            "final_virtual_clock_ms",
            "final_projection_hash",
            "final_state_hash",
            "frame_hashes_hash",
        )
    }
    tick_evidence = None
    tick_coverage = None
    if args.tick_evidence is not None:
        tick_evidence = {
            "path": str(args.tick_evidence),
            "sha256": _sha256(args.tick_evidence),
            "manifest": json.loads(args.tick_evidence.read_text(encoding="utf-8")),
        }
        if tick_evidence["manifest"].get("trade_date") != args.trade_date:
            raise ValueError("tick evidence trade_date does not match Q2 replay")
        tick_symbols = {
            normalize
            for item in tick_evidence["manifest"].get("expected_symbols", ())
            if (normalize := str(item).zfill(6))
        }
        q2_symbols = set(inventory["symbols"])
        missing_q2_symbols = sorted(tick_symbols - q2_symbols)
        tick_coverage = {
            "expected_count": len(tick_symbols),
            "observed_q2_symbol_count": len(tick_symbols & q2_symbols),
            "missing_q2_symbol_count": len(missing_q2_symbols),
            "missing_q2_symbols_sample": missing_q2_symbols[:20],
            "coverage": (
                len(tick_symbols & q2_symbols) / len(tick_symbols)
                if tick_symbols else 0.0
            ),
        }

    report = {
        "contract_version": "Task008T1V2Q2FrameReplayV1",
        "trade_date": args.trade_date,
        "q2frame": {"path": str(args.q2frame), "sha256": _sha256(args.q2frame)},
        "inventory": inventory,
        "ordered": first,
        "repeat": second,
        "determinism": comparison,
        "deterministic": all(comparison.values()),
        "tick_evidence": tick_evidence,
        "tick_q2_coverage": tick_coverage,
        "same_input_provenance": "DECLARED_BY_SHARED_TD_WINDOW_NOT_ROW_HASH_BOUND",
        "q2_source": "t1-v2",
        "q2_time_policy": "SOURCE_TIME_ONLY; NO_MARKET_HOURS_GATE",
        "historical_available_at": "UNKNOWN",
        "production_side_effects": "NONE_OBSERVED",
        "replay_status": "REPLAY_READY_BOUNDED" if all(comparison.values()) else "REPLAY_NON_DETERMINISTIC",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "frames": inventory["frame_count"],
        "updates": inventory["update_count"],
        "symbols": inventory["symbol_count"],
        "deterministic": report["deterministic"],
        "replay_status": report["replay_status"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if report["deterministic"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
