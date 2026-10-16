#!/usr/bin/env python3
"""Import explicitly selected reference files into immutable Core artifacts."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from engine_core.data_pipeline.storage import ArtifactCatalog


def _json_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [dict(row) for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("rows")
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, dict)]
        if isinstance(payload.get("plates"), list):
            return [dict(row) for row in payload["plates"] if isinstance(row, dict)]
        if isinstance(payload.get("response"), dict) and isinstance(payload["response"].get("list"), list):
            return [dict(row, captured_local=payload.get("captured_local")) if isinstance(row, dict)
                    else {"raw": row, "captured_local": payload.get("captured_local")}
                    for row in payload["response"]["list"]]
        return [dict(value, code=key) if isinstance(value, dict) else {"code": key, "value": value}
                for key, value in payload.items() if key not in {"captured_local", "response"}]
    return []


def _rows(path: Path, dataset: str = "") -> list[dict[str, Any]]:
    if path.is_dir():
        if dataset == "plate_registry":
            return _rows(path / "registry.json")
        if dataset == "plate_members":
            return _rows(path / "relation.csv")
        if dataset == "limit_reasons":
            rows = []
            for item in sorted(path.glob("reasons/*.json")):
                payload = json.loads(item.read_text(encoding="utf-8"))
                symbol = str(payload.get("StockID") or item.stem).zfill(6)
                for reason in payload.get("List", []):
                    if isinstance(reason, dict):
                        rows.append({**reason, "symbol": symbol})
            return rows
        if dataset == "limit_up_pool":
            rows = []
            for item in sorted(path.glob("pools/*_ban*.json")):
                date, _, level = item.stem.rpartition("_ban")
                payload = json.loads(item.read_text(encoding="utf-8"))
                info = payload.get("info", []) if isinstance(payload, dict) else []
                raw_rows = info[0] if info and isinstance(info[0], list) else []
                for raw in raw_rows:
                    if isinstance(raw, list) and raw:
                        rows.append({"date": date, "symbol": str(raw[0]).zfill(6),
                                     "code": str(raw[0]).zfill(6),
                                     "name": str(raw[1]) if len(raw) > 1 else None,
                                     "plate_label": raw[5] if len(raw) > 5 else None,
                                     "theme_text": raw[12] if len(raw) > 12 else None,
                                     "board_height": raw[15] if len(raw) > 15 else None,
                                     "theme_codes": raw[19] if len(raw) > 19 else None,
                                     "lb_days": int(level) if level.isdigit() else None, "raw": raw})
            return rows
        if dataset == "hot_plates":
            rows = []
            for item in sorted(path.glob("hot_plates/*.json")):
                payload = json.loads(item.read_text(encoding="utf-8"))
                captured = payload.get("captured_local") or payload.get("available_at")
                if not captured and payload.get("Time"):
                    captured = datetime.fromtimestamp(float(payload["Time"])).astimezone().isoformat()
                response = payload.get("response", payload)
                for raw in response.get("list", []) if isinstance(response, dict) else []:
                    if isinstance(raw, list) and len(raw) >= 9:
                        rows.append({"date": str(captured or "")[:10], "available_at": captured,
                                     "code": str(raw[0]), "name": str(raw[1]),
                                     "strength": raw[2], "change_pct": raw[3],
                                     "amount_yuan": raw[5], "net_yuan": raw[6], "raw": raw})
            return rows
        raise ValueError(f"directory_input_requires_supported_dataset:{dataset}")
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return _json_rows(path)


def import_files(mappings: list[str], *, catalog: ArtifactCatalog, trade_date: str,
                 as_of: str, pit_date: str | None, execute: bool = False) -> list[dict[str, Any]]:
    output = []
    for mapping in mappings:
        dataset, separator, raw_path = mapping.partition("=")
        if not separator or not dataset or not raw_path:
            raise ValueError("input_must_be_dataset_equals_path")
        path = Path(raw_path).expanduser().resolve()
        if path.is_dir():
            digest_input = b"".join(item.relative_to(path).as_posix().encode("utf-8") + b"\0" +
                                     item.read_bytes() + b"\0"
                                     for item in sorted(path.rglob("*")) if item.is_file())
        else:
            digest_input = path.read_bytes()
        digest = hashlib.sha256(digest_input).hexdigest()
        rows = _rows(path, dataset)
        for row in rows:
            row.setdefault("available_at", as_of)
            if pit_date:
                row.setdefault("date", pit_date)
                row.setdefault("pit_date", pit_date)
        manifest = {"trade_date": trade_date, "as_of": as_of, "available_at": as_of,
                    "point_in_time_date": pit_date, "provenance": "import_from_validation",
                    "source_sha256": digest, "source_path": str(path),
                    "row_count": len(rows), "status": "READY"}
        written = catalog.commit(dataset, trade_date, rows, manifest, source="import_from_validation") if execute else None
        output.append({"dataset": dataset, "path": str(path), "rows": len(rows),
                       "source_sha256": digest, "execute": execute,
                       "version": written.get("version") if written else None})
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", default=[], metavar="DATASET=PATH", required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--pit-date")
    parser.add_argument("--root", default="~/core-data")
    parser.add_argument("--execute", action="store_true", help="commit immutable artifacts; default is dry-run")
    args = parser.parse_args(argv)
    catalog = ArtifactCatalog(Path(args.root).expanduser())
    results = import_files(args.input, catalog=catalog, trade_date=args.trade_date,
                           as_of=args.as_of, pit_date=args.pit_date, execute=args.execute)
    print(json.dumps({"mode": "execute" if args.execute else "dry-run", "root": str(catalog.root),
                      "datasets": results}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
