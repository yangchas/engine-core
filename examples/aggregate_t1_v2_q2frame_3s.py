"""Collapse t1-v2 Q2 source batches into project 3-second replay frames.

All source frames are read and all updates are retained in the manifest. Within
one 3-second half-open bucket, only the latest update per symbol is emitted;
Q2FrameReplaySource carries the prior symbol state across buckets. This is a
timeline adapter, not a Q2 recomputation or a market-hours filter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--bucket-ms", type=int, default=3000)
    args = parser.parse_args()
    if args.bucket_ms <= 0:
        parser.error("bucket-ms must be positive")

    source_frames = source_updates = 0
    output_frames = output_updates = 0
    first_ts = last_ts = None
    anchor = None
    bucket_no = None
    pending: dict[str, dict[str, Any]] = {}

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.input.open("r", encoding="utf-8") as source, args.output.open("w", encoding="utf-8") as target:
        def flush() -> None:
            nonlocal output_frames, output_updates, bucket_no, pending
            if bucket_no is None:
                return
            logical_ts_ms = anchor + (bucket_no + 1) * args.bucket_ms
            updates = [pending[symbol] for symbol in sorted(pending)]
            target.write(json.dumps({
                "version": "Q2FrameV1",
                "seq_no": output_frames + 1,
                "logical_ts_ms": logical_ts_ms,
                "q2_updates": updates,
            }, ensure_ascii=False, separators=(",", ":")) + "\n")
            output_frames += 1
            output_updates += len(updates)
            pending = {}

        def flush_empty(empty_bucket: int) -> None:
            nonlocal output_frames
            logical_ts_ms = anchor + (empty_bucket + 1) * args.bucket_ms
            target.write(json.dumps({
                "version": "Q2FrameV1",
                "seq_no": output_frames + 1,
                "logical_ts_ms": logical_ts_ms,
                "q2_updates": [],
            }, ensure_ascii=False, separators=(",", ":")) + "\n")
            output_frames += 1

        for line_no, line in enumerate(source, 1):
            if not line.strip():
                continue
            frame = json.loads(line)
            if frame.get("version") != "Q2FrameV1":
                raise ValueError(f"line {line_no}: unsupported Q2Frame version")
            logical_ts_ms = int(frame["logical_ts_ms"])
            updates = frame.get("q2_updates", [])
            if anchor is None:
                anchor = logical_ts_ms
                first_ts = logical_ts_ms
            if last_ts is not None and logical_ts_ms < last_ts:
                raise ValueError("source Q2Frame timestamps moved backwards")
            last_ts = logical_ts_ms
            source_frames += 1
            source_updates += len(updates)
            current_bucket = (logical_ts_ms - anchor) // args.bucket_ms
            if bucket_no is None:
                bucket_no = current_bucket
            elif current_bucket != bucket_no:
                if current_bucket < bucket_no:
                    raise ValueError("source Q2Frame bucket moved backwards")
                flush()
                for empty_bucket in range(bucket_no + 1, current_bucket):
                    flush_empty(empty_bucket)
                bucket_no = current_bucket
            for update in updates:
                if not isinstance(update, dict) or "symbol" not in update:
                    raise ValueError(f"line {line_no}: invalid q2 update")
                pending[str(update["symbol"])] = update
        flush()

    digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
    report = {
        "contract_version": "Task008T1V2Q2Frame3sAggregationV1",
        "input": str(args.input),
        "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
        "output": str(args.output),
        "output_sha256": digest,
        "bucket_ms": args.bucket_ms,
        "source_frame_count": source_frames,
        "source_update_count": source_updates,
        "output_frame_count": output_frames,
        "output_update_count": output_updates,
        "first_source_ts_ms": first_ts,
        "last_source_ts_ms": last_ts,
        "logical_ts_policy": "bucket end boundary; source time remains in update payload",
        "production_side_effects": "NONE_OBSERVED",
    }
    args.manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
