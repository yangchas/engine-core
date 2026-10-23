#!/usr/bin/env python3
"""Capture a RAW upstream response as a real test fixture, with provenance.

Fixtures under tests/fixtures/real/ must come from here (or from copying a committed Core artifact via
--from-catalog); hand-written rows are not accepted by tests/test_real_fixtures.py.

  capture_real_fixture.py ths_hot_rank
  capture_real_fixture.py kaipan_hot_plates [--date YYYY-MM-DD]
  capture_real_fixture.py kaipan_pool [--date YYYY-MM-DD] [--level 1]
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
REAL = ROOT / "tests" / "fixtures" / "real"
PROVENANCE = REAL / "PROVENANCE.json"
TZ = ZoneInfo("Asia/Shanghai")


def _ths_raw() -> tuple[object, dict]:
    from engine_core.data_pipeline.providers import ths_hot_rank as ths
    params = {"stock_type": "a", "type": "day", "list_type": "tech"}
    url = ths.URL + "?" + urllib.parse.urlencode(params)
    last = None
    for _ in range(4):  # the endpoint is flaky (SSL handshake timeouts observed 2026-10-23)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=ths.HEADERS), timeout=30) as response:
                return json.loads(response.read().decode("utf-8")), {"endpoint": ths.URL, "params": params}
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            time.sleep(5)
    raise SystemExit(f"capture failed: {last!r}")


def _kaipan(call: str, **kwargs) -> object:
    return getattr(importlib.import_module("pykaipan.pykaipan"), call)(**kwargs)


def capture(kind: str, day: str, level: int) -> tuple[str, object, dict]:
    if kind == "ths_hot_rank":
        raw, meta = _ths_raw()
        return "ths_hot_rank", raw, meta
    if kind == "kaipan_hot_plates":
        params = {"date": day, "index": "0", "size": "50"}
        return "kaipan_hot_plates", _kaipan("getHisPlates", **params), {"call": "getHisPlates", "params": params}
    if kind == "kaipan_pool":
        params = {"date": day, "ban": str(level), "size": "2000"}
        return "kaipan_pool", _kaipan("getHisBans", **params), {"call": "getHisBans", "params": params}
    raise SystemExit("unknown kind " + kind)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("kind", choices=["ths_hot_rank", "kaipan_hot_plates", "kaipan_pool"])
    parser.add_argument("--date", default=datetime.now(TZ).date().isoformat())
    parser.add_argument("--level", type=int, default=1)
    args = parser.parse_args(argv)
    fetched_at = datetime.now(TZ).isoformat(timespec="seconds")
    source, raw, meta = capture(args.kind, args.date, args.level)
    body = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str, indent=1).encode("utf-8") + b"\n"
    level = f"_L{args.level}" if args.kind == "kaipan_pool" else ""
    name = f"{source}_{args.date}{level}_{fetched_at[11:16].replace(':', '')}.json"
    REAL.mkdir(parents=True, exist_ok=True)
    (REAL / name).write_bytes(body)
    index = json.loads(PROVENANCE.read_text(encoding="utf-8")) if PROVENANCE.exists() else {}
    index[name] = {"source": source, "trade_date": args.date, "fetched_at": fetched_at, "bytes": len(body),
                   "sha256": hashlib.sha256(body).hexdigest(), "kind": "raw_upstream_response",
                   "command": "examples/capture_real_fixture.py " + " ".join(argv or sys.argv[1:]), **meta}
    PROVENANCE.write_text(json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(name, len(body), "bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
