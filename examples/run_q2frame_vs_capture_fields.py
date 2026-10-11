"""Compare a t1-v2 Q2Frame replay with a sealed live Q2 capture over all Q2 fields.

Offline and read-only: it reads a local Q2Frame JSONL file and a sealed
``RedisQ2ReadCaptureV1`` JSON file.  Frames are folded through
``Q2FrameReplaySource`` (the same Core path used by the Engine replay); the final
projection's per-symbol raw fields are compared with the capture's hash fields.

Time alignment is classified before values: a replay whose source ``ts`` is newer or
older than the capture's is reported separately from a same-``ts`` value disagreement.
Fields that are absent on one side are UNKNOWN, never zero.  The capture wall read
time is an observation only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core import FreshnessPolicy, Q2FrameReplaySource, VirtualClock, canonical_json  # noqa: E402
from engine_core.replay import Q2FrameV1  # noqa: E402

CONTRACT_VERSION = "Q2FrameVsCaptureFieldsV1"
SAMPLE_LIMIT = 20


def load_capture_hashes(path: Path) -> tuple[dict[str, Any], dict[str, Mapping[str, str]]]:
    capture = json.loads(path.read_text(encoding="utf-8"))
    hashes: dict[str, Mapping[str, str]] = {}
    for entry in capture["reads"]:
        key = entry.get("key", "")
        if (
            entry.get("operation") == "hgetall"
            and key.startswith("q2:")
            and not key.startswith("q2:active")
            and entry.get("value")
        ):
            hashes[key[3:]] = entry["value"]
    return capture, hashes


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def replay_final_projection(path: Path, trade_date: str, expected_symbols: list[str]):
    source: Optional[Q2FrameReplaySource] = None
    projection = None
    frames = updates = 0
    first_ts = last_ts = None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            frame = Q2FrameV1.from_mapping(json.loads(line))
            if source is None:
                first_ts = frame.logical_ts_ms
                source = Q2FrameReplaySource(
                    trade_date,
                    expected_symbols,
                    VirtualClock(datetime.fromtimestamp(frame.logical_ts_ms / 1000.0, timezone.utc)),
                    freshness_policy=FreshnessPolicy(stale_after_ms=60_000, max_future_skew_ms=999),
                    source_id="t1_v2_q2frame",
                )
            projection = source.apply(frame)
            frames += 1
            updates += len(frame.q2_updates)
            last_ts = frame.logical_ts_ms
    if projection is None:
        raise ValueError("Q2Frame file has no frames")
    return projection, {
        "frame_count": frames, "update_count": updates,
        "first_logical_ts_ms": first_ts, "last_logical_ts_ms": last_ts,
    }


def compare_fields(projection: Any, hashes: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
    status: dict[str, Counter] = {}
    by_relation: dict[str, Counter] = {}
    samples: dict[str, list] = {}
    relation = Counter()
    no_replay_symbols = []
    for symbol, capture in sorted(hashes.items()):
        quote = projection.quotes.get(symbol)
        replay = {str(k): str(v) for k, v in dict(quote.raw_fields).items()} if quote is not None else None
        if replay is None:
            no_replay_symbols.append(symbol)
            rel = "NO_REPLAY_STATE"
        else:
            cap_ts, rep_ts = _int_or_none(capture.get("ts")), _int_or_none(replay.get("ts"))
            if cap_ts is None or rep_ts is None:
                rel = "TS_UNKNOWN"
            elif rep_ts > cap_ts:
                rel = "REPLAY_SOURCE_NEWER"
            elif rep_ts < cap_ts:
                rel = "REPLAY_SOURCE_OLDER"
            else:
                rel = "SAME_TS"
        relation[rel] += 1
        for field in sorted(set(capture) | set(replay or ())):
            left, right = (replay or {}).get(field), capture.get(field)
            if left is None or right is None:
                result = "UNKNOWN"
            else:
                result = "MATCH" if left == str(right) else "MISMATCH"
            status.setdefault(field, Counter())[result] += 1
            by_relation.setdefault(field, Counter())[rel + ":" + result] += 1
            if result != "MATCH" and len(samples.setdefault(field + ":" + result, [])) < SAMPLE_LIMIT:
                samples[field + ":" + result].append(
                    {"symbol": symbol, "replay": left, "capture": right, "relation": rel}
                )
    same_ts_all = sum(
        1 for s, c in hashes.items()
        if s in projection.quotes
        and all(
            str(dict(projection.quotes[s].raw_fields).get(f)) == str(v)
            for f, v in c.items()
        )
        and _int_or_none(dict(projection.quotes[s].raw_fields).get("ts")) == _int_or_none(c.get("ts"))
    )
    return {
        "capture_symbols": len(hashes),
        "relation_counts": dict(relation),
        "relation_sum_equals_symbols": sum(relation.values()) == len(hashes),
        "symbols_with_every_capture_field_equal": same_ts_all,
        "field_status_counts": {k: dict(v) for k, v in sorted(status.items())},
        "field_status_by_relation": {k: dict(v) for k, v in sorted(by_relation.items())},
        "no_replay_state_symbols": no_replay_symbols[:50],
        "samples": samples,
    }


def run(*, q2frame: Path, capture_path: Path, trade_date: str, out_dir: Path) -> dict[str, Any]:
    started = time.time()
    capture, hashes = load_capture_hashes(capture_path)
    projection, frame_info = replay_final_projection(q2frame, trade_date, sorted(hashes))
    again, _ = replay_final_projection(q2frame, trade_date, sorted(hashes))
    comparison = compare_fields(projection, hashes)
    comparison["projection_content_hash"] = projection.content_hash
    comparison["projection_deterministic_on_repeat"] = projection.content_hash == again.content_hash
    out_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "contract_version": CONTRACT_VERSION, "trade_date": trade_date,
        "q2frame": {"path": str(q2frame), "sha256": hashlib.sha256(q2frame.read_bytes()).hexdigest(), **frame_info},
        "capture": {
            "path": str(capture_path), "sha256": hashlib.sha256(capture_path.read_bytes()).hexdigest(),
            "wall_read_observed": [capture.get("read_started_at"), capture.get("read_completed_at")],
            "wall_time_policy": "OBSERVED_ONLY",
        },
        "elapsed_seconds": round(time.time() - started, 2),
        "limits": [
            "Q2Frame is produced by t1-v2 from TD rows; the live Redis hashes come from the same live process family",
            "event-time replay order is not the live processing order; no Rabbit/available_at/09:25:06 claim",
            "final-state comparison; intermediate updates are not compared",
        ],
    }
    for name, payload in (("manifest.json", manifest), ("field_comparison.json", comparison)):
        with (out_dir / name).open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=1, sort_keys=True, default=str))
    sums = [
        "%s  %s" % (hashlib.sha256(p.read_bytes()).hexdigest(), p.name) for p in sorted(out_dir.iterdir())
    ]
    with (out_dir / "sha256sums.txt").open("x", encoding="utf-8") as handle:
        handle.write("\n".join(sums) + "\n")
    return {"manifest": manifest, "comparison": comparison}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--q2frame", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run(q2frame=args.q2frame, capture_path=args.capture, trade_date=args.trade_date, out_dir=args.out_dir)
    print(canonical_json({
        "relations": result["comparison"]["relation_counts"],
        "fields": result["comparison"]["field_status_counts"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
