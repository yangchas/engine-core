"""Build a local StockDayFacts snapshot from catalogued files only."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

from engine_core.stock_day_facts import build_facts, read_facts, write_facts


def _load(path: Path):
    if path.is_dir():
        path = path / "daily_kline.jsonl"
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if path.suffix == ".json":
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj.get("stocks", obj.get("rows", obj)) if isinstance(obj, dict) else obj
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--stage", choices=("auction", "open", "close"), required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--reference-root", default="/home/exedev/validation")
    parser.add_argument("--reference-catalog", required=True)
    parser.add_argument("--previous-facts")
    args = parser.parse_args(argv)
    catalog_path = Path(args.reference_catalog)
    if not catalog_path.is_absolute():
        catalog_path = Path(args.reference_root) / catalog_path
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    refs = catalog.get("references", catalog)
    inputs = {}
    for key in ("stocks", "names", "caps", "relations", "yesterday_pool", "auction", "checkpoints", "daily", "daily_history"):
        entry = refs.get(key)
        raw = entry.get("path") if isinstance(entry, dict) else entry
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = catalog_path.parent / path
        if path.exists():
            inputs[key] = _load(path)
    previous = read_facts(args.previous_facts)[0] if args.previous_facts else None
    rows = build_facts(inputs, trade_date=args.trade_date, stage=args.stage, previous_rows=previous)
    source_hash = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    manifest = write_facts(rows, args.out_dir, trade_date=args.trade_date,
                           stage=args.stage, source_manifest_sha256=source_hash)
    print(f"trade_date={args.trade_date} stage={args.stage} rows={len(rows)} revision={manifest['revision']} sha256={manifest['sha256']} build_seconds={manifest['build_seconds']:.6f} out={args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
