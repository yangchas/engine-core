#!/usr/bin/env python3
"""Export one read-only Redis q2 active set and its hashes to a gzip fixture."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "q2_snapshot"
PROVENANCE = FIXTURE_DIR / "PROVENANCE.json"
TZ = ZoneInfo("Asia/Shanghai")


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_index(index: dict) -> None:
    tmp = PROVENANCE.with_name("PROVENANCE.json.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(PROVENANCE)


def _export(day: str) -> dict:
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        raise RuntimeError("REDIS_URL_required")
    import redis
    from engine_core.data_pipeline.intraday_checkpoint import read_q2

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    index = json.loads(PROVENANCE.read_text(encoding="utf-8")) if PROVENANCE.exists() else {"snapshots": {}}
    index.setdefault("snapshots", {})
    if day in index["snapshots"]:
        return index["snapshots"][day]

    client = redis.Redis.from_url(redis_url, decode_responses=True,
                                  socket_connect_timeout=5, socket_timeout=15)
    try:
        symbols, hashes = read_q2(client, day)
    finally:
        client.close()
    if not symbols:
        raise RuntimeError("q2_active_set_empty")
    captured_at = datetime.now(TZ).isoformat(timespec="seconds")
    snapshot = {
        "trade_date": day,
        "captured_at": captured_at,
        "active_symbols": symbols,
        "hashes": {symbol: hashes[symbol] for symbol in symbols},
    }
    compressed = bytearray()
    import io
    with io.BytesIO() as buffer:
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as handle:
            handle.write(_json_bytes(snapshot))
        compressed.extend(buffer.getvalue())
    filename = f"q2_{day.replace('-', '')}.json.gz"
    path = FIXTURE_DIR / filename
    path.write_bytes(compressed)
    capture = {
        "source": "Redis q2 active set and hashes (read only)", "trade_date": day,
        "captured_at": captured_at, "command": "SMEMBERS q2:active:YYYYMMDD; HGETALL q2:{symbol}",
        "active_symbols": len(symbols), "hashes": len(hashes), "file": filename,
        "bytes": len(compressed), "sha256": hashlib.sha256(compressed).hexdigest(),
    }
    index["snapshots"][day] = capture
    _write_index(index)
    return capture


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=datetime.now(TZ).date().isoformat())
    args = parser.parse_args(argv)
    try:
        capture = _export(args.date)
    except Exception as exc:
        detail = str(exc) if str(exc) in {"REDIS_URL_required", "q2_active_set_empty"} else "redis_read_failed"
        print(f"snapshot failed: {type(exc).__name__}: {detail}", file=sys.stderr)
        return 1
    print(json.dumps({"trade_date": capture["trade_date"], "active_symbols": capture["active_symbols"],
                      "hashes": capture["hashes"], "file": capture["file"],
                      "sha256": capture["sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
