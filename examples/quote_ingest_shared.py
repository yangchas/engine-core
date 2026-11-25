#!/usr/bin/env python3
"""Poll the controlled shared Q2 output and optionally persist Core-owned segments."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from engine_core.data_pipeline.storage import canonical_json
from engine_core.quote_ingest.shared_source import SharedQuoteSource


DAY_CAP_BYTES = 2 * 1024**3


def connect_redis():
    import redis
    url = os.environ.get("REDIS_URL")
    if not url:
        raise RuntimeError("REDIS_URL is required")
    return redis.Redis.from_url(url, decode_responses=True)


def _as_row(state):
    value = dict(state.__dict__)
    for key in ("ts", "read_at", "available_at"):
        value[key] = value[key].isoformat() if value[key] else None
    value["quality"] = list(value["quality"])
    return value


def _existing_bytes(day_root):
    return sum(path.stat().st_size for path in day_root.rglob("*") if path.is_file()) if day_root.exists() else 0


def _disk_use(path):
    usage = shutil.disk_usage(path if path.exists() else path.parent)
    return usage.used / usage.total


def _read_final_snapshot(root, trade_date):
    base = root / "q2_final" / f"trade_date={trade_date}"
    manifests = sorted(base.glob("source=*/sha256-*/manifest.json"))
    if not manifests:
        return None
    manifest_path = manifests[-1]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows_path = Path(manifest.get("rows_uri", manifest_path.parent / "rows.jsonl.gz"))
    with gzip.open(rows_path, "rt", encoding="utf-8") as stream:
        return manifest, {str(row["symbol"]).zfill(6): row for row in map(json.loads, stream) if row.get("symbol")}


def _reconcile_snapshot(states, root, trade_date):
    previous = _read_final_snapshot(root, trade_date)
    if previous is None:
        return {"q2_final": {"status": "MISSING"},
                "td_stock_tick_v2": {"status": "NOT_COMPARABLE_NO_SHARED_OBSERVATION_WINDOW"}}
    manifest, frozen = previous
    rows = {row.symbol: row for row in states}
    symbols = sorted(set(rows) & set(frozen))[:200]
    errors = {"price_milli": [], "amount_yuan": []}
    for symbol in symbols:
        current, old = rows[symbol], frozen[symbol]
        for field, left, right in (
            ("price_milli", current.price_milli, old.get("px")),
            ("amount_yuan", current.amount_yuan, old.get("amt")),
        ):
            try:
                if left is not None and right is not None:
                    errors[field].append(abs(int(left) - int(right)))
            except (TypeError, ValueError):
                pass
    def quantiles(values):
        values.sort()
        if not values: return {"n": 0, "median_abs": None, "p95_abs": None, "max_abs": None}
        return {"n": len(values), "median_abs": values[(len(values)-1)//2],
                "p95_abs": values[min(len(values)-1, int(.95*(len(values)-1)))], "max_abs": values[-1]}
    return {"q2_final": {"status": "COMPARED", "snapshot_sha256": manifest.get("sha256"),
                         "snapshot_captured_at": manifest.get("captured_at"), "sample_symbols": len(symbols),
                         "price_abs_delta_milli": quantiles(errors["price_milli"]),
                         "amount_abs_delta_yuan": quantiles(errors["amount_yuan"])},
            "td_stock_tick_v2": {"status": "NOT_COMPARABLE_NO_SHARED_OBSERVATION_WINDOW",
                                  "note": "Q2 current read and historical 09:30-09:45 TD cut have different observation times"}}


def sample_once(redis, trade_date, *, root=None, reference_root=None, write=False, previous=None):
    started = time.monotonic()
    read = SharedQuoteSource(redis=redis, trade_date=trade_date).read_since()
    states = read.rows
    changed = [row for row in states if previous is None or previous.get(row.symbol) != row.raw]
    reference_root = reference_root or root
    reconciliation = _reconcile_snapshot(states, reference_root, trade_date) if reference_root else {}
    rows_bytes = b"".join(canonical_json(_as_row(row)) + b"\n" for row in changed)
    result = {"trade_date": trade_date, "row_count": len(states), "changed_count": len(changed),
              "missing_hashes": read.missing_symbols, "partial": read.partial,
              "read_start": read.read_start.isoformat(), "read_end": read.read_end.isoformat(),
              "elapsed_seconds": round(time.monotonic() - started, 3),
              "uncompressed_bytes": len(rows_bytes), "reconciliation": reconciliation}
    if write:
        if root is None:
            raise ValueError("--root is required with --write")
        day_root = root / "quote_stream" / f"trade_date={trade_date}"
        day_root.mkdir(parents=True, exist_ok=True)
        if _disk_use(day_root) >= .90:
            raise RuntimeError("write refused: disk usage is at least 90%")
        if _existing_bytes(day_root) + len(rows_bytes) > DAY_CAP_BYTES:
            raise RuntimeError("write stopped: daily quote_stream cap exceeded")
        segment = day_root / f"seg={read.read_start.astimezone().strftime('%H%M%S-%f')}"
        segment.mkdir(parents=True, exist_ok=False)
        compressed = gzip.compress(rows_bytes, mtime=0)
        payload_path = segment / "rows.jsonl.gz"
        payload_path.write_bytes(compressed)
        digest = hashlib.sha256(compressed).hexdigest()
        manifest = {**result, "source": "legacy_shared_q2", "source_quality": "LEGACY_SHARED_PARTIAL_POSSIBLE",
                    "incremental_completeness": "UNKNOWN", "read_start": read.read_start.isoformat(),
                    "read_end": read.read_end.isoformat(), "ts_min": min((r.ts.isoformat() for r in changed if r.ts), default=None),
                    "ts_max": max((r.ts.isoformat() for r in changed if r.ts), default=None),
                    "rows_sha256": digest, "compressed_bytes": len(compressed),
                    "estimated_full_day_uncompressed_bytes_at_3s": len(rows_bytes) * 7200,
                    "daily_cap_bytes": DAY_CAP_BYTES, "reconciliation": reconciliation}
        (segment / "manifest.json").write_bytes(canonical_json(manifest) + b"\n")
        result.update({"segment": str(segment), "compressed_bytes": len(compressed), "rows_sha256": digest})
    return result, {row.symbol: row.raw for row in states}


def main(argv=None):
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=3)
    parser.add_argument("--trade-date", default=datetime.now().astimezone().date().isoformat())
    parser.add_argument("--root", type=Path, default=Path(os.environ["CORE_DATA_ROOT"]) if os.getenv("CORE_DATA_ROOT") else None)
    parser.add_argument("--reference-root", type=Path, default=Path(os.environ["CORE_DATA_ROOT"]) if os.getenv("CORE_DATA_ROOT") else None)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if args.write and args.root is None: parser.error("--root or CORE_DATA_ROOT required with --write")
    if args.interval_seconds <= 0: parser.error("interval must be positive")
    redis = connect_redis()
    previous = None
    while True:
        result, previous = sample_once(redis, args.trade_date, root=args.root,
                                        reference_root=args.reference_root, write=args.write, previous=previous)
        print(json.dumps(result, sort_keys=True))
        if not args.run: break
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
