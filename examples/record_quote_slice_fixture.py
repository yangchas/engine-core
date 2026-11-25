#!/usr/bin/env python3
"""Record the real inputs of capture_quote_slice (Redis q2 keys + TD auction_snapshot_v2) for replay in tests.

    record_quote_slice_fixture.py 2026-11-03 --tag 0925 --out tests/fixtures/real/c662

Read-only: Redis SMEMBERS/HGETALL/HGET/TYPE and one TD SELECT. Writes three gzip JSON files shaped like the
tests/replay_quote_slice.py loaders expect, and their entries in tests/fixtures/real/PROVENANCE.json
(`_c662_recordings`). The q2 hashes are the terminal state at recording time, not a point-in-time 09:25 value.
"""
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
from engine_core.data_pipeline.capture.quote_slice import TD_COLUMNS  # noqa: E402

PROVENANCE = ROOT / "tests" / "fixtures" / "real" / "PROVENANCE.json"


def _text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _write(path: Path, payload) -> bytes:
    body = gzip.compress(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"), mtime=0)
    path.write_bytes(body)
    return body


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trade_date")
    parser.add_argument("--tag", default="0925")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    import redis
    from engine_core import plate_amount_baseline

    day = args.trade_date.replace("-", "")
    client = redis.Redis.from_url(os.environ.get("REDIS_URL") or "redis://127.0.0.1:6379/0", socket_timeout=10)
    symbols = sorted(_text(item) for item in client.smembers(f"q2:active:{day}"))
    if not symbols:
        print(f"no q2:active:{day} in Redis", file=sys.stderr)
        return 2
    now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    auction = {f"q2:active:{day}": {"type": "set", "value": symbols}}
    for key in (f"a2:{day}:{args.tag}", f"market:auction:{day}:{args.tag}"):
        if client.exists(key):
            auction[key] = {"type": _text(client.type(key)),
                            "value": {_text(k): _text(v) for k, v in client.hgetall(key).items()}}
    q2 = {}
    for start in range(0, len(symbols), 500):
        pipe = client.pipeline(transaction=False)
        batch = symbols[start:start + 500]
        for symbol in batch:
            pipe.hgetall(f"q2:{symbol}")
        for symbol, raw in zip(batch, pipe.execute()):
            if raw:
                q2[symbol] = {"type": "hash", "value": {_text(k): _text(v) for k, v in raw.items()}}
    sql = ("select ts,px_milli,chg_bp,match_amt_yuan,rest_bid_amt_yuan,rest_ask_amt_yuan,"
           "limit_state,symbol,trade_date,auction_tag from market_data1.auction_snapshot_v2 "
           f"where trade_date='{day}' and auction_tag='{args.tag}'")
    connection = plate_amount_baseline._td_connect()
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        rows = [[str(v) if i == 0 else v for i, v in enumerate(row)] for row in cursor.fetchall()]
        cursor.close()
    finally:
        connection.close()
    td = {"read_at": now, "query": sql, "cols": list(TD_COLUMNS), "n": len(rows), "rows": rows}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    names = {"redis_auction_keys": (auction, f"redis_auction_keys_{day}.json.gz"),
             "redis_q2_hashes": (q2, f"redis_q2_hashes_{day}.json.gz"),
             "td_auction_snapshot_v2": (td, f"td_auction_snapshot_v2_{day}.json.gz")}
    provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    section = provenance.setdefault("_c662_recordings", {})
    for source, (payload, name) in names.items():
        body = _write(out / name, payload)
        section[f"{out.name}/{name}"] = {
            "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "kind": "raw_upstream_recording",
            "source": source, "trade_date": args.trade_date, "fetched_at": now,
            "command": f"examples/record_quote_slice_fixture.py {args.trade_date} --tag {args.tag}",
            "note": "q2 hashes are the state at recording time, not a point-in-time 09:25 value"
                    if source == "redis_q2_hashes" else "read-only recording"}
    PROVENANCE.write_text(json.dumps(provenance, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"symbols": len(symbols), "q2_hashes": len(q2), "td_rows": len(rows)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
