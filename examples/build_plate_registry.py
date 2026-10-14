"""Build dated plate registry and compact point-in-time relation artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from engine_core.plate_registry import build_registry, build_relation_arrays
from engine_core.plate_static_relation import parse_plate_inner_edges, parse_stock_plate_csv


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _parse_as_of(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("as_of must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("as_of must be YYYY-MM-DD")
    return value


def build(csv_dir: Path, dictionary_path: Path, output_dir: Path, *, as_of: str,
          generic_names_file: Path | None = None) -> None:
    """Build registry JSON, CSV/NPZ relations, and an input/output manifest."""
    as_of = _parse_as_of(as_of)
    if output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir())):
        raise ValueError(f"output directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    relation_path = csv_dir / "个股板块.csv"
    plates_path = csv_dir / "板块.csv"
    relation_bytes = relation_path.read_bytes()
    plates_bytes = plates_path.read_bytes()
    dictionary_bytes = dictionary_path.read_bytes()
    dictionary: dict[str, Any] = json.loads(dictionary_bytes.decode("utf-8"))

    relation_counts: dict[str, int] = {}
    relation = parse_stock_plate_csv(relation_bytes, counts=relation_counts)
    csv_edges = parse_plate_inner_edges(plates_bytes)
    names = dict(dictionary.get("names", {}))
    unnamed_codes = set(map(str, dictionary.get("unnamed_codes", ())))
    for code in unnamed_codes:
        names.pop(code, None)
    edges = list(dictionary.get("edges", ())) + csv_edges
    parent_of: dict[str, str] = {}
    for edge in edges:
        if len(edge) >= 2:
            parent_of.setdefault(str(edge[1]), str(edge[0]))

    generic_names: list[str] = []
    if generic_names_file is not None:
        generic_payload = json.loads(generic_names_file.read_text(encoding="utf-8"))
        generic_names = generic_payload.get("names", []) if isinstance(generic_payload, dict) else generic_payload
        if not isinstance(generic_names, list) or not all(isinstance(name, str) for name in generic_names):
            raise ValueError("generic names file must contain a names array of strings")
    registry = build_registry(relation, names, parent_of, generic_names=generic_names)
    generic_name_set = set(generic_names)
    if generic_name_set:
        for row in registry["plates"]:
            if row["name"] in generic_name_set:
                row["class"] = "GENERIC_FACTOR"
                if "GENERIC_FACTOR" not in row["flags"]:
                    row["flags"].append("GENERIC_FACTOR")
                if "GENERIC_NAMES_FILE" not in row["flags"]:
                    row["flags"].append("GENERIC_NAMES_FILE")
    arrays = build_relation_arrays(relation)
    registry_path = output_dir / "registry.json"
    registry_path.write_text(
        json.dumps(registry, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with (output_dir / "relation.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("plateid", "stockid"))
        for symbol_index, plate_index in arrays["pairs"]:
            writer.writerow((arrays["plates"][plate_index], arrays["symbols"][symbol_index]))

    npz_status = "written"
    try:
        import numpy as np
    except ModuleNotFoundError as exc:
        if exc.name != "numpy":
            raise
        npz_status = "skipped"
    else:
        stock_idx = np.asarray([pair[0] for pair in arrays["pairs"]], dtype=np.int32)
        plate_idx = np.asarray([pair[1] for pair in arrays["pairs"]], dtype=np.int32)
        np.savez_compressed(
            output_dir / "relation.npz",
            stock_idx=stock_idx,
            plate_idx=plate_idx,
            symbols=np.asarray(arrays["symbols"], dtype=str),
            plates=np.asarray(arrays["plates"], dtype=str),
        )

    class_counts = dict(sorted(Counter(row["class"] for row in registry["plates"]).items()))
    manifest = {
        "as_of": as_of,
        "inputs": {
            "个股板块.csv": {"sha256": _sha256(relation_bytes)},
            "板块.csv": {"sha256": _sha256(plates_bytes)},
            "dictionary.json": {"sha256": _sha256(dictionary_bytes)},
        },
        "plate_count": len(registry["plates"]),
        "class_counts": class_counts,
        "family_count": len({row["family"] for row in registry["plates"]}),
        "unnamed_plate_count": sum(row["name"] is None for row in registry["plates"]),
        "symbol_count": len(arrays["symbols"]),
        "member_pair_count": len(arrays["pairs"]),
        "npz": npz_status,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-dir", type=Path, required=True)
    parser.add_argument("--dictionary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--generic-names-file", type=Path)
    args = parser.parse_args(argv)
    build(args.csv_dir, args.dictionary, args.output_dir, as_of=args.as_of,
          generic_names_file=args.generic_names_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
