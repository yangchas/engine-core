#!/usr/bin/env python3
"""Copy a REAL TD error out of a guardian ledger record into tests/fixtures/real/td/errors/<name>.json.

  capture_td_error.py NAME LEDGER_JSONL --seq N
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("ledger")
    parser.add_argument("--seq", type=int, required=True)
    args = parser.parse_args(argv)
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
