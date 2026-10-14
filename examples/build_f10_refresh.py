#!/usr/bin/env python3
"""Build a dated F10 CSV from a legacy CSV and local Wencai fetch artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine_core.f10_refresh import merge_f10, normalize_wencai_rows


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _load_caps(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("caps", payload)


def run(old_csv: Path, wencai_dirs: list[Path], caps_json: Path | None, new_asof: str,
        output_dir: Path) -> None:
    started = datetime.now(timezone.utc).isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", new_asof):
        raise ValueError("--new-asof must be YYYY-MM-DD")
    if output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir())):
        raise FileExistsError(f"output directory must be nonexistent or empty: {output_dir}")
    with old_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        old_columns = list(reader.fieldnames or [])
        old_rows = list(reader)
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    input_files = [old_csv]
    for folder in wencai_dirs:
        rows_path = folder / "rows.json"
        manifest_path = folder / "manifest.json"
        if not rows_path.is_file() or not manifest_path.is_file():
            raise FileNotFoundError(f"expected rows.json and manifest.json in {folder}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("complete") is False:
            raise ValueError(f"incomplete Wencai artifact: {folder}")
        label = folder.name.lower()
        group = next((g for g in ("mkt", "ind", "fin") if re.search(rf"(?:^|-){g}(?:_|-|$)", label)), None)
        if group is None:
            raise ValueError(f"cannot infer mkt/ind/fin group from directory name: {folder.name}")
        raw_rows = json.loads(rows_path.read_text(encoding="utf-8"))
        normalized = normalize_wencai_rows(raw_rows, group)
        declared_units = manifest.get("field_units", manifest.get("units", {}))
        if isinstance(declared_units, dict) and declared_units:
            for row in normalized.values():
                row.setdefault("_units", {}).update(declared_units)
        target = grouped.setdefault(group, {})
        for symbol, row in normalized.items():
            if symbol in target:
                prior = target[symbol]
                # Merge duplicate symbol rows from segmented artifacts, latest dated field wins.
                for key, value in row.items():
                    if key.startswith("_") or key == "股票代码":
                        continue
                    prior_date = prior.get("_asof_by_column", {}).get(key, "")
                    date = row.get("_asof_by_column", {}).get(key, row.get("_asof") or "")
                    if key not in prior or date >= prior_date:
                        prior[key] = value
                        prior.setdefault("_asof_by_column", {})[key] = date
                prior["_asof"] = max(prior.get("_asof") or "", row.get("_asof") or "") or None
            else:
                target[symbol] = row
        input_files.extend((rows_path, manifest_path))
    if caps_json:
        input_files.append(caps_json)
    rows, report = merge_f10(old_rows, grouped, _load_caps(caps_json), new_asof=new_asof)
    output_dir.mkdir(parents=True, exist_ok=True)
    extra_columns = ["_asof_mkt", "_source_mkt"]
    columns = old_columns + [c for c in extra_columns if c not in old_columns]
    with (output_dir / "f10_refreshed.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            output_row = {c: row.get(c) for c in old_columns}
            cap_col = "a股市值(不含限售股)"
            output_row["_asof_mkt"] = row.get("_asof_by_column", {}).get(cap_col)
            output_row["_source_mkt"] = row.get("_source", {}).get(cap_col)
            writer.writerow(output_row)
    provenance = {"asof": new_asof, "columns": report["source_counts"]}
    _json_write(output_dir / "f10_provenance.json", provenance)
    _json_write(output_dir / "coverage_report.json", report)
    ended = datetime.now(timezone.utc).isoformat()
    _json_write(output_dir / "manifest.json", {
        "schema": "F10RefreshedV1", "new_asof": new_asof,
        "inputs": [{"path": str(p), "sha256": _sha256(p)} for p in input_files],
        "row_count": len(rows), "started_at": started, "ended_at": ended,
    })
    print(f"rows={len(rows)} added={report['added_symbols']} retained={report['retained_symbols']} updated={report['updated_symbols']}")
    print(f"outputs={output_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-csv", type=Path, required=True)
    parser.add_argument("--wencai-dirs", required=True, help="comma-separated local artifact directories")
    parser.add_argument("--caps-json", type=Path)
    parser.add_argument("--new-asof", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    dirs = [Path(part) for part in args.wencai_dirs.split(",") if part.strip()]
    run(args.old_csv, dirs, args.caps_json, args.new_asof, args.output_dir)


if __name__ == "__main__":
    main()
