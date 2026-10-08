"""Repeat a frozen Q2Frame artifact through the generic streaming replay helper.

The input is scanned sequentially for inventory, then streamed twice in source
order through one Engine per pass. Only the unique symbol universe and output
hashes are retained; frame rows are not accumulated. This tool reads local
files only and does not connect to Redis, TDengine, RabbitMQ, or services.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping
from zoneinfo import ZoneInfo

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
    replay_q2frames,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def _iter_raw(path: Path) -> Iterator[Mapping[str, Any]]:
    with _open_text(path) as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise ValueError(f"Q2Frame line {line_number} is not an object")
            yield value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inventory(path: Path) -> dict[str, Any]:
    frame_count = update_count = empty_frame_count = 0
    subsecond_frame_count = 0
    symbols: set[str] = set()
    first_time_ms = last_time_ms = None
    last_seq_no = 0
    for raw in _iter_raw(path):
        frame = Q2FrameV1.from_mapping(raw)
        if frame.seq_no != last_seq_no + 1:
            raise ValueError("Q2Frame seq_no is not continuous")
        if last_time_ms is not None and frame.logical_ts_ms < last_time_ms:
            raise ValueError("Q2Frame logical timestamps moved backwards")
        frame_count += 1
        update_count += len(frame.q2_updates)
        empty_frame_count += int(not frame.q2_updates)
        subsecond_frame_count += int(frame.logical_ts_ms % 1000 != 0)
        symbols.update(str(update["symbol"]) for update in frame.q2_updates)
        first_time_ms = frame.logical_ts_ms if first_time_ms is None else first_time_ms
        last_time_ms = frame.logical_ts_ms
        last_seq_no = frame.seq_no
    if not frame_count:
        raise ValueError("Q2Frame artifact is empty")
    ordered_symbols = tuple(sorted(symbols))
    return {
        "frame_count": frame_count,
        "update_count": update_count,
        "empty_frame_count": empty_frame_count,
        "subsecond_logical_frame_count": subsecond_frame_count,
        "observed_symbol_count": len(ordered_symbols),
        "observed_symbol_set_hash": canonical_hash(ordered_symbols),
        "universe_basis": "UNIQUE_SYMBOLS_IN_FROZEN_Q2FRAME_ONLY",
        "first_logical_ts_ms": first_time_ms,
        "last_logical_ts_ms": last_time_ms,
        "symbols": ordered_symbols,
    }


class _HashingReplaySource(Q2FrameReplaySource):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.frame_hash_chain = hashlib.sha256()
        self.projection_hash_chain = hashlib.sha256()
        self.frame_count = 0

    def signal_for(self, raw_frame, *, signal_prefix="q2frame"):
        frame_hash = canonical_hash(raw_frame)
        signal = super().signal_for(raw_frame, signal_prefix=signal_prefix)
        self.frame_hash_chain.update(bytes.fromhex(frame_hash))
        self.projection_hash_chain.update(bytes.fromhex(signal.payload.content_hash))
        self.frame_count += 1
        return signal


def _engine(trade_date: str) -> DeterministicEngine:
    return DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("streaming_q2frame", 0, 10**16),)),
        ProbeStrategy(),
        session_id=f"STREAMING-Q2FRAME:{trade_date}",
        phase="REPLAY",
    )


def _run_once(
    path: Path,
    trade_date: str,
    inventory: Mapping[str, Any],
    *,
    run_name: str,
    heartbeat_frames: int,
) -> dict[str, Any]:
    initial_time_ms = int(inventory["first_logical_ts_ms"])
    initial_time_ms -= initial_time_ms % 1000
    initial = datetime.fromtimestamp(initial_time_ms / 1000, timezone.utc)
    clock = VirtualClock(initial)
    source = _HashingReplaySource(
        trade_date,
        inventory["symbols"],
        clock,
        source_id="t1_v2_q2frame_streaming_audit",
    )
    engine = _engine(trade_date)
    started = time.monotonic()

    def frames() -> Iterator[Mapping[str, Any]]:
        for index, raw in enumerate(_iter_raw(path), 1):
            if heartbeat_frames and index % heartbeat_frames == 0:
                print(
                    f"{run_name}: input_frames_read={index} "
                    f"elapsed_seconds={time.monotonic() - started:.1f}",
                    flush=True,
                )
            yield raw

    replay_q2frames(
        frames(),
        source,
        engine,
        signal_prefix="t1-v2-q2frame-streaming-audit",
        end_logical_time_ms=int(inventory["last_logical_ts_ms"]),
    )
    state = engine._reducer.state
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
        "run_name": run_name,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "input_frames_consumed": source.frame_count,
        "input_frame_hash_chain": source.frame_hash_chain.hexdigest(),
        "projection_hash_chain": source.projection_hash_chain.hexdigest(),
        "processed_signals": engine._processed,
        "reducer_revision": state.revision,
        "virtual_clock_ms": int(clock.now_utc().timestamp() * 1000),
        "final_state_hash": final_state_hash,
        "pending_engine_signals": len(engine._queue),
    }


def _runtime_provenance() -> dict[str, Any]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    source_files = sorted((ROOT / "src/engine_core").glob("*.py"))
    return {
        "git_head": head,
        "git_worktree_dirty": bool(status),
        "git_dirty_path_count": len(status),
        "engine_core_source_sha256": {
            str(path.relative_to(ROOT)): _file_sha256(path)
            for path in source_files
        },
        "runner_sha256": _file_sha256(Path(__file__).resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--heartbeat-frames", type=int, default=100)
    args = parser.parse_args()

    input_path = args.input.resolve()
    output_dir = args.output_dir.resolve()
    observed_sha256 = _file_sha256(input_path)
    if observed_sha256.lower() != args.expected_sha256.lower():
        raise SystemExit(
            f"input SHA-256 mismatch: expected {args.expected_sha256}, "
            f"observed {observed_sha256}"
        )
    output_dir.mkdir(parents=True, exist_ok=False)
    inventory = _inventory(input_path)
    provenance = _runtime_provenance()
    run_a = _run_once(
        input_path,
        args.trade_date,
        inventory,
        run_name="ordered-1",
        heartbeat_frames=args.heartbeat_frames,
    )
    run_b = _run_once(
        input_path,
        args.trade_date,
        inventory,
        run_name="ordered-2",
        heartbeat_frames=args.heartbeat_frames,
    )
    compared_fields = (
        "input_frames_consumed",
        "input_frame_hash_chain",
        "projection_hash_chain",
        "processed_signals",
        "reducer_revision",
        "virtual_clock_ms",
        "final_state_hash",
        "pending_engine_signals",
    )
    deterministic = all(run_a[field] == run_b[field] for field in compared_fields)
    report = {
        "contract": "Q2FrameStreamingRepeatAuditV1",
        "generated_at": datetime.now(SHANGHAI).isoformat(),
        "result": "DETERMINISTIC_REPEAT_PASS" if deterministic else "NON_DETERMINISTIC",
        "input": {
            "path": str(input_path),
            "sha256": observed_sha256,
            "trade_date": args.trade_date,
        },
        "inventory": {
            key: value for key, value in inventory.items() if key != "symbols"
        },
        "replay_contract": {
            "mode": "LOCAL_FROZEN_REAL_Q2FRAME_EVENT_TIME_REPLAY",
            "ordering": "SOURCE_FILE_ORDER; NOT_RABBIT_ARRIVAL_ORDER",
            "expected_universe": inventory["universe_basis"],
            "engine_instances_per_run": 1,
            "engine_session_id": f"STREAMING-Q2FRAME:{args.trade_date}",
            "engine_phase": "REPLAY",
            "window": {"name": "streaming_q2frame", "start_ms": 0, "end_ms": 10**16},
            "source_id": "t1_v2_q2frame_streaming_audit",
            "signal_prefix": "t1-v2-q2frame-streaming-audit",
            "end_logical_time_ms": inventory["last_logical_ts_ms"],
            "runs": "same input order and identical configuration, independently reconstructed source and Engine",
        },
        "runs": [run_a, run_b],
        "limits": {
            "rabbit_arrival_order": "UNKNOWN",
            "historical_available_at": "UNKNOWN",
            "full_market_coverage": "UNPROVEN",
            "normal_opening_acceptance": "NOT_EVALUATED",
            "production_side_effects": "NONE_OBSERVED",
            "production_services_accessed": False,
        },
        "provenance": provenance,
    }
    report_path = output_dir / "replay_summary.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    audit_path = output_dir / "replay_audit.md"
    audit_path.write_text(
        "# Q2Frame streaming replay repeat audit\n\n"
        f"- Result: `{report['result']}`\n"
        f"- Input SHA-256: `{observed_sha256}`\n"
        f"- Frames / updates / observed symbols: "
        f"{inventory['frame_count']} / {inventory['update_count']} / "
        f"{inventory['observed_symbol_count']}\n"
        f"- Ordered repeat final hashes: `{run_a['final_state_hash']}` / "
        f"`{run_b['final_state_hash']}`\n"
        f"- Signals / revisions: {run_a['processed_signals']} / "
        f"{run_a['reducer_revision']} per run\n"
        "- Replay order: source file order; Rabbit arrival remains UNKNOWN.\n"
        "- Full-market coverage: UNPROVEN. This is not NORMAL acceptance.\n"
        "- No live Redis/TD/Rabbit/service access or production effects.\n\n"
        "See `replay_summary.json` for configuration, checksums, and per-run details.\n",
        encoding="utf-8",
    )
    sums = []
    for path in (report_path, audit_path):
        sums.append(f"{_file_sha256(path)}  {path.name}")
    sums.append(f"{observed_sha256}  {input_path}")
    (output_dir / "sha256sums.txt").write_text(
        "\n".join(sums) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"result": report["result"], "runs": report["runs"]}, indent=2))
    return 0 if deterministic else 2


if __name__ == "__main__":
    raise SystemExit(main())
