#!/usr/bin/env python3
"""Copy a REAL TD error out of a guardian ledger record into tests/fixtures/real/td/errors/<name>.json.

  capture_td_error.py NAME LEDGER_JSONL --seq N
  capture_td_error.py NAME --provoke "SELECT ... FROM a_table_that_does_not_exist"   # needs TD_CONFIG
The error text is taken verbatim from the ledger record's text by matching the `taos.error...: [0x....]: message`
pattern; the record's seq, timestamp and sha256 are stored as provenance and registered in PROVENANCE.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "real" / "td" / "errors"
PROVENANCE = ROOT / "tests" / "fixtures" / "real" / "PROVENANCE.json"
PATTERN = re.compile(r"(\[0x[0-9a-fA-F]{4}\]: [^\n\"\\]+)")


def provoke(name: str, sql: str) -> int:
    import os
    from datetime import datetime
    from zoneinfo import ZoneInfo
    if not re.match(r"^\s*select\b", sql, re.IGNORECASE) or not os.environ.get("TD_CONFIG"):
        print("a SELECT and TD_CONFIG are required", file=sys.stderr)
        return 2
    sys.path.insert(0, str(ROOT / "src"))
    from engine_core import plate_amount_baseline as module
    cursor = module._td_connect().cursor()
    try:
        cursor.execute(sql)
        print("TD accepted the statement; nothing to record", file=sys.stderr)
        return 3
    except Exception as exc:  # the real client exception, recorded verbatim
        message, client_exception = str(exc).strip(), type(exc).__name__
    match = PATTERN.search(message)
    if not match:
        print("no TD error text: " + message[:200], file=sys.stderr)
        return 2
    text = match.group(1).strip()
    now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    body = {"errno": text[1:7], "message": text, "source": {"provoked_by": sql, "recorded_at": now,
            "client_exception": client_exception}}
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / f"{name}.json"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    provenance.setdefault("_td_recordings", {})[f"td/errors/{name}.json"] = {
        "kind": "raw_td_error", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size,
        "command": f"examples/capture_td_error.py {name} --provoke {sql!r}", "source": body["source"]}
    PROVENANCE.write_text(json.dumps(provenance, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(text)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("ledger", nargs="?")
    parser.add_argument("--seq", type=int)
    parser.add_argument("--provoke", help="read-only statement that TD is expected to reject; its real error is recorded")
    args = parser.parse_args(argv)
    if args.provoke:
        return provoke(args.name, args.provoke)
    if not args.ledger or args.seq is None:
        print("ledger and --seq are required (or --provoke)", file=sys.stderr)
        return 2
    record = next((line for line in Path(args.ledger).read_text(encoding="utf-8").splitlines()
                   if json.loads(line).get("seq") == args.seq), None)
    if record is None:
        print("seq not found", file=sys.stderr)
        return 2
    match = PATTERN.search(record)
    if not match:
        print("no TD error text in that record", file=sys.stderr)
        return 2
    message = match.group(1).strip()
    body = {"errno": message[1:7], "message": message,
            "source": {"ledger": str(args.ledger), "seq": args.seq, "ts": json.loads(record).get("ts"),
                       "record_sha256": hashlib.sha256(record.encode("utf-8")).hexdigest()}}
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / f"{args.name}.json"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    provenance.setdefault("_td_recordings", {})[f"td/errors/{args.name}.json"] = {
        "kind": "raw_td_error", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size,
        "command": f"examples/capture_td_error.py {args.name} {args.ledger} --seq {args.seq}",
        "source": body["source"]}
    PROVENANCE.write_text(json.dumps(provenance, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
