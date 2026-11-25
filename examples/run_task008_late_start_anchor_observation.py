"""Replay one sealed Redis Q2 hash capture as one late-start Q2Frame.

This is an offline evidence runner. It reads local, already captured artifacts;
it has no Redis, TDengine, RabbitMQ, or production-service client. The Redis
read wall time is recorded as observation metadata only and is never promoted
to historical availability.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from engine_core.q2 import normalize_q2  # noqa: E402
from examples.run_task008_q2frame_auction_engine_shadow import (  # noqa: E402
    run_q2frame_auction_engine_shadow,
)

CAPTURE_PATH = Path(
    "/home/exedev/validation/open-live-followup-20261009T094227+0800/"
    "probe_claude_live_capture.json"
)
BASELINE_PATH = Path(
    "/home/exedev/validation/task008-q2frame-classifier-merge-check-Hxjfs5/"
    "result.json"
)
EXPECTED_CAPTURE_SHA256 = "7806d6d1dfd26daa4807ae0512708b21b37bf6c6d351104be364a70fd8d02003"
EXPECTED_BASELINE_SHA256 = "8b2098f584fdcf909fa1f55c003c60589c8b294c2356e61dd460607a70bf7a79"
SOURCE_LAYER = "redis_q2_sealed_hash_snapshot"
SOURCE_ID = "redis_q2_sealed_snapshot_20261009"
SIGNAL_PREFIX = "redis-q2-sealed-snapshot"
SAMPLE_LIMIT = 20


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse_epoch_ms(value: Any) -> int | None:
    try:
        normalized = normalize_q2("000001", {"ts": value}).source_record_time_ms
    except (TypeError, ValueError):
        return None
    return normalized if isinstance(normalized, int) and normalized > 0 else None


def capture_to_single_q2frame(
    capture: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project captured SMEMBERS/HGETALL results without inventing rows.

    Hash field names and values are retained exactly as captured. The frame
    clock is the latest valid per-symbol source timestamp truncated to a whole
    second, not the Redis read wall time.
    """

    if capture.get("contract_version") != "RedisQ2ReadCaptureV1":
        raise ValueError("unsupported Redis capture contract")
    trade_date = capture.get("trade_date")
    if not isinstance(trade_date, str) or len(trade_date) != 10:
        raise ValueError("capture trade_date must be YYYY-MM-DD")
    compact_date = trade_date.replace("-", "")
    reads = capture.get("reads")
    if not isinstance(reads, list):
        raise ValueError("capture reads must be an array")

    active_entries = [
        item
        for item in reads
        if isinstance(item, Mapping)
        and item.get("key") == f"q2:active:{compact_date}"
        and item.get("operation") == "smembers"
    ]
    if len(active_entries) != 1:
        raise ValueError("capture must contain exactly one Q2 active-set read")
    raw_members = active_entries[0].get("value")
    if not isinstance(raw_members, list):
        raise ValueError("Q2 active-set value must be an array")
    active_members = {str(member).strip() for member in raw_members if str(member).strip()}

    hashes: dict[str, Mapping[str, Any]] = {}
    for item in reads:
        if not isinstance(item, Mapping) or item.get("operation") != "hgetall":
            continue
        key = item.get("key")
        if not isinstance(key, str) or not key.startswith("q2:") or key.startswith("q2:active:"):
            continue
        symbol = key[3:]
        if not symbol:
            continue
        if symbol in hashes:
            raise ValueError(f"duplicate captured Q2 hash for {symbol}")
        value = item.get("value")
        if not isinstance(value, Mapping):
            raise ValueError(f"captured Q2 hash for {symbol} is not a mapping")
        if any(not isinstance(field, str) for field in value):
            raise ValueError(f"captured Q2 hash field names must be strings: {symbol}")
        hashes[symbol] = value

    active_with_hash = active_members.intersection(hashes)
    active_without_hash = active_members.difference(hashes)
    hash_outside_active = set(hashes).difference(active_members)
    source_times: dict[str, int] = {}
    invalid_or_missing_ts: list[str] = []
    updates: list[dict[str, Any]] = []
    field_presence: Counter[str] = Counter()
    zero_anchor_counts: Counter[str] = Counter()
    for symbol in sorted(active_with_hash):
        raw_fields = hashes[symbol]
        if any(not isinstance(value, str) for value in raw_fields.values()):
            raise ValueError(f"captured Redis hash values must be strings: {symbol}")
        normalized = normalize_q2(symbol, raw_fields)
        if normalized.source_record_time_ms is None:
            invalid_or_missing_ts.append(symbol)
        else:
            source_times[symbol] = normalized.source_record_time_ms
        for field in raw_fields:
            field_presence[str(field)] += 1
        for anchor_field in ("a20", "a24", "a25"):
            if raw_fields.get(anchor_field) == "0":
                zero_anchor_counts[anchor_field] += 1
        updates.append({"symbol": normalized.symbol, **dict(raw_fields)})

    if not source_times:
        raise ValueError("capture has no valid per-symbol Q2 source timestamp")
    first_source_ms = min(source_times.values())
    latest_source_ms = max(source_times.values())
    logical_ts_ms = (latest_source_ms // 1000) * 1000
    stale_after_ms = capture.get("freshness_policy_stale_after_ms")
    if isinstance(stale_after_ms, bool) or not isinstance(stale_after_ms, int) or stale_after_ms < 0:
        stale_after_ms = None
    stale_symbol_count = (
        sum(1 for source_ms in source_times.values() if logical_ts_ms - source_ms > stale_after_ms)
        if stale_after_ms is not None
        else None
    )

    frame = {
        "version": "Q2FrameV1",
        "seq_no": 1,
        "logical_ts_ms": logical_ts_ms,
        "q2_updates": updates,
    }
    diagnostics = {
        "trade_date": trade_date,
        "active_member_count": len(active_members),
        "captured_hash_count": len(hashes),
        "active_member_with_hash_count": len(active_with_hash),
        "active_member_without_hash_count": len(active_without_hash),
        "active_member_without_hash_samples": sorted(active_without_hash)[:SAMPLE_LIMIT],
        "hash_outside_active_count": len(hash_outside_active),
        "hash_outside_active_samples": sorted(hash_outside_active)[:SAMPLE_LIMIT],
        "frame_update_count": len(updates),
        "invalid_or_missing_source_ts_count": len(invalid_or_missing_ts),
        "invalid_or_missing_source_ts_samples": invalid_or_missing_ts[:SAMPLE_LIMIT],
        "source_time_min_ms": first_source_ms,
        "source_time_max_ms": latest_source_ms,
        "logical_ts_ms": logical_ts_ms,
        "source_time_older_than_capture_frame_count": stale_symbol_count,
        "freshness_policy_stale_after_ms": stale_after_ms,
        "field_presence_counts": dict(sorted(field_presence.items())),
        "zero_anchor_field_counts": dict(sorted(zero_anchor_counts.items())),
        "source_time_interpretation": "per-symbol Q2 ts; not Rabbit arrival or anchor-field available_at",
        "logical_time_policy": "maximum valid per-symbol source timestamp, truncated to whole seconds",
        "observation_time_mode": capture.get("observation_time_mode"),
        "read_started_at": capture.get("read_started_at"),
        "read_completed_at": capture.get("read_completed_at"),
        "read_wall_time_policy": "OBSERVED_ONLY_NOT_HISTORICAL_AVAILABLE_AT",
        "active_set_is_full_market_authority": False,
    }
    return frame, diagnostics


def compare_anchor_facts(
    baseline_result: Mapping[str, Any], replay_result: Mapping[str, Any]
) -> dict[str, Any]:
    baseline_tags = baseline_result.get("ordered", {}).get("anchor_evidence", {})
    replay_tags = replay_result.get("ordered", {}).get("anchor_evidence", {})
    result: dict[str, Any] = {
        "contract": "Task008LateStartAnchorFactComparisonV1",
        "classification": "DIAGNOSTIC_ONLY_NOT_HISTORICAL_CUTOFF_PARITY",
        "compared_fields": ["status", "price_milli"],
        "revision_ids_and_hashes_compared": False,
        "tags": {},
    }
    for tag in ("0920", "0924", "0925"):
        baseline_facts = baseline_tags.get(tag, {}).get("auction_anchor_facts_by_symbol", {})
        replay_facts = replay_tags.get(tag, {}).get("auction_anchor_facts_by_symbol", {})
        baseline_symbols = set(baseline_facts)
        replay_symbols = set(replay_facts)
        common = baseline_symbols & replay_symbols
        same = []
        mismatch_samples: list[dict[str, Any]] = []
        mismatch_count = 0
        for symbol in sorted(common):
            before = baseline_facts[symbol]
            after = replay_facts[symbol]
            before_pair = (before.get("status"), before.get("price_milli"))
            after_pair = (after.get("status"), after.get("price_milli"))
            if before_pair == after_pair:
                same.append(symbol)
            else:
                mismatch_count += 1
                if len(mismatch_samples) < SAMPLE_LIMIT:
                    mismatch_samples.append(
                        {
                            "symbol": symbol,
                            "baseline": {"status": before_pair[0], "price_milli": before_pair[1]},
                            "sealed_snapshot": {"status": after_pair[0], "price_milli": after_pair[1]},
                        }
                    )
        result["tags"][tag] = {
            "baseline_fact_count": len(baseline_symbols),
            "sealed_snapshot_fact_count": len(replay_symbols),
            "common_symbol_count": len(common),
            "same_status_and_price_count": len(same),
            "different_status_or_price_count": mismatch_count,
            "baseline_only_count": len(baseline_symbols - replay_symbols),
            "snapshot_only_count": len(replay_symbols - baseline_symbols),
            "mismatch_samples": mismatch_samples,
        }
    return result


def _write_json(path: Path, payload: Any) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=1, default=str)
        handle.write("\n")


def run(*, capture_path: Path, baseline_path: Path, out_dir: Path) -> dict[str, Any]:
    started = time.monotonic()
    capture_bytes = capture_path.read_bytes()
    capture_sha = sha256_bytes(capture_bytes)
    if capture_sha != EXPECTED_CAPTURE_SHA256:
        raise ValueError(f"sealed Redis capture SHA mismatch: {capture_sha}")
    baseline_bytes = baseline_path.read_bytes()
    baseline_sha = sha256_bytes(baseline_bytes)
    if baseline_sha != EXPECTED_BASELINE_SHA256:
        raise ValueError(f"full replay baseline SHA mismatch: {baseline_sha}")
    capture = json.loads(capture_bytes)
    baseline = json.loads(baseline_bytes)
    frame, capture_inventory = capture_to_single_q2frame(capture)
    trade_date = str(capture_inventory["trade_date"])
    if baseline.get("trade_date") != trade_date:
        raise ValueError("baseline and sealed Redis capture trade dates differ")
    out_dir.mkdir(parents=True, exist_ok=False)

    frame_path = out_dir / "sealed_q2_snapshot_as_one_frame.jsonl"
    frame_bytes = (
        json.dumps(frame, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    with frame_path.open("xb") as handle:
        handle.write(frame_bytes)
    frame_sha = sha256_bytes(frame_bytes)
    result = run_q2frame_auction_engine_shadow(
        q2frame_path=frame_path,
        trade_date=trade_date,
        expected_sha256=frame_sha,
        q2frame_source="sealed Redis Q2 HGETALL snapshot converted to exactly one Q2FrameV1",
        input_kind="SEALED_REDIS_Q2_HASH_SNAPSHOT_AS_SINGLE_Q2FRAME",
        q2frame_source_id=SOURCE_ID,
        q2frame_source_layer=SOURCE_LAYER,
        q2frame_signal_prefix=SIGNAL_PREFIX,
        observe_persistent_anchors_on_late_start=True,
        run_mode="SEALED_Q2_REDIS_HASH_SNAPSHOT_LATE_START_ANCHOR_OBSERVATION",
    )
    comparison = compare_anchor_facts(baseline, result)
    result_path = out_dir / "late_start_result.json"
    _write_json(result_path, result)
    comparison_path = out_dir / "anchor_comparison.json"
    _write_json(comparison_path, comparison)
    capture_inventory.update(
        {
            "capture_path": str(capture_path),
            "capture_sha256": capture_sha,
            "capture_declared_input_canonical_sha256": capture.get("input_canonical_sha256"),
            "baseline_path": str(baseline_path),
            "baseline_sha256": baseline_sha,
            "q2frame_path": str(frame_path),
            "q2frame_sha256": frame_sha,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "current_code_revision": _git_revision(),
            "code_worktree_status": _git_status(),
            "implementation_source_sha256": {
                relative: sha256_file(ROOT / relative)
                for relative in (
                    "examples/run_task008_q2frame_auction_engine_shadow.py",
                    "examples/run_task008_late_start_anchor_observation.py",
                    "tests/test_task008_q2frame_auction_engine_shadow.py",
                    "tests/test_task008_q2frame_bounded_window.py",
                    "tests/test_task008_late_start_anchor_observation.py",
                )
            },
            "limits": [
                "sealed Redis snapshot is one observation cohort, not original Rabbit batches or arrival order",
                "per-symbol Q2 ts is source time; it does not prove when a20/a24/a25 became available",
                "capture read wall time is observed only and is not historical available_at",
                "comparison with full replay reports per-symbol status/price only; it is diagnostic, not cutoff equivalence",
                "q2:active membership is not independently proven to be full-market coverage",
                "late-start anchor observation inspects only the first input group; sparse later-field retry is a separate policy",
                "capture-declared canonical input digest is recorded but not recomputed; exact capture file SHA-256 is pinned",
            ],
        }
    )
    _write_json(out_dir / "input_manifest.json", capture_inventory)
    report = _report_text(capture_inventory, result, comparison)
    with (out_dir / "report.md").open("x", encoding="utf-8") as handle:
        handle.write(report)
    files = sorted(path for path in out_dir.iterdir() if path.is_file())
    with (out_dir / "sha256sums.txt").open("x", encoding="utf-8") as handle:
        for path in files:
            handle.write(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n")
    return {
        "out_dir": str(out_dir),
        "deterministic": result.get("deterministic"),
        "input_frames_processed": result.get("ordered", {}).get("input_frames_processed"),
        "input_updates_processed": result.get("ordered", {}).get("input_updates_processed"),
        "comparison": comparison,
    }


def _git_revision() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _git_status() -> list[str] | None:
    try:
        output = subprocess.check_output(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [line for line in output.splitlines() if line.strip()]


def _report_text(
    manifest: Mapping[str, Any], result: Mapping[str, Any], comparison: Mapping[str, Any]
) -> str:
    ordered = result.get("ordered", {})
    anchors = ordered.get("anchor_evidence", {})
    lines = [
        "# TASK-008 late-start sealed Q2 snapshot observation",
        "",
        f"Trade date: `{manifest['trade_date']}`",
        f"Capture SHA-256: `{manifest['capture_sha256']}`",
        f"Baseline full-replay SHA-256: `{manifest['baseline_sha256']}`",
        f"One-frame SHA-256: `{manifest['q2frame_sha256']}`",
        f"Code HEAD: `{manifest['current_code_revision']}`; worktree status: `{manifest['code_worktree_status']}`",
        "",
        "## Run result",
        "",
        f"- Contract: `{result.get('contract_version')}`",
        f"- Deterministic ordered/repeat: `{result.get('deterministic')}`",
        f"- Frames / updates: `{ordered.get('input_frames_processed')}` / `{ordered.get('input_updates_processed')}`",
        f"- Virtual clock: `{ordered.get('virtual_clock_ms')}`",
        f"- Active members / captured hashes / frame updates: `{manifest['active_member_count']}` / `{manifest['captured_hash_count']}` / `{manifest['frame_update_count']}`",
        f"- Source timestamp min/max: `{manifest['source_time_min_ms']}` / `{manifest['source_time_max_ms']}`; logical time is whole-second-truncated max source time `{manifest['logical_ts_ms']}`.",
        f"- Source times older than freshness threshold: `{manifest['source_time_older_than_capture_frame_count']}` of observed rows (threshold `{manifest['freshness_policy_stale_after_ms']}` ms).",
        "- Redis read wall time is `OBSERVED_ONLY_NOT_HISTORICAL_AVAILABLE_AT`; historical availability remains unknown.",
        "- No Redis/TD/Rabbit connections or writes were performed by this runner; it consumed local sealed artifacts only.",
        "",
        "## Anchor observations",
        "",
    ]
    for tag in ("0920", "0924", "0925"):
        anchor = anchors.get(tag, {})
        revision = anchor.get("auction_revision", {})
        observed = result.get("ordered", {}).get("late_start_observation_by_tag", {}).get(tag, {})
        lines.append(
            f"- `{tag}`: observation `{observed.get('status')}`, eval `{revision.get('evaluation_time_ms')}`, late execution `{revision.get('late_execution')}`, source layers `{revision.get('source_layers')}`."
        )
    lines.extend(["", "## Baseline comparison (diagnostic only)", ""])
    for tag, values in comparison.get("tags", {}).items():
        lines.append(
            f"- `{tag}`: common `{values['common_symbol_count']}`, same status+price `{values['same_status_and_price_count']}`, different `{values['different_status_or_price_count']}`, baseline-only `{values['baseline_only_count']}`, snapshot-only `{values['snapshot_only_count']}`."
        )
    lines.extend(
        [
            "",
            "This comparison does not claim historical cutoff parity: the baseline is event-time replay evidence, while this input is one later Redis hash snapshot. Revision IDs and hashes are intentionally not compared.",
            "",
            "## Limits",
            "",
            *[f"- {item}" for item in manifest["limits"]],
            "- Auction evidence remains `FACT_ONLY`; this run does not prove NORMAL acceptance or live Rabbit equivalence.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, default=CAPTURE_PATH)
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    outcome = run(capture_path=args.capture, baseline_path=args.baseline, out_dir=args.out_dir)
    print(json.dumps(outcome, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
