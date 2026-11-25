"""Build a local JSON artifact from uploaded Kaipan static plate CSV files."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from engine_core.plate_static_relation import (
    build_static_relation,
    parse_plate_inner_edges,
    parse_stock_plate_csv,
    static_relation_to_dict,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build(csv_dir: Path, dictionary_path: Path, output_dir: Path) -> None:
    if output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir())):
        raise ValueError(f"output directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    relation_path = csv_dir / "个股板块.csv"
    plates_path = csv_dir / "板块.csv"
    relation_bytes, plates_bytes, dictionary_bytes = (
        relation_path.read_bytes(), plates_path.read_bytes(), dictionary_path.read_bytes(),
    )
    dictionary: dict[str, Any] = json.loads(dictionary_bytes.decode("utf-8"))
    relation_counts: dict[str, int] = {}
    inner_counts: dict[str, int] = {}
    relation_by_plate = parse_stock_plate_csv(relation_bytes, counts=relation_counts)
    csv_edges = parse_plate_inner_edges(plates_bytes, counts=inner_counts)
    names = dictionary.get("names", {})
    dict_edges = dictionary.get("edges", [])
    edges = list(dict_edges) + csv_edges
    result = build_static_relation(
        relation_by_plate, names, edges, unnamed_codes=dictionary.get("unnamed_codes", ()),
    )
    (output_dir / "static_relation.json").write_text(
        json.dumps(static_relation_to_dict(result), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "files": {
            "个股板块.csv": {"sha256": _sha256(relation_bytes), "rows": relation_counts["rows"]},
            "板块.csv": {"sha256": _sha256(plates_bytes), "rows": inner_counts["rows"]},
            "dictionary.json": {"sha256": _sha256(dictionary_bytes),
                                 "name_rows": len(names), "edge_rows": len(dict_edges)},
        },
        "relation": {
            **relation_counts, "plate_count": len(relation_by_plate),
            "member_pair_count": sum(map(len, relation_by_plate.values())),
        },
        "inner": inner_counts,
        "symbol_count": len(result.by_symbol),
        "hierarchy_edge_count": len(result.hierarchy.parent_by_child),
        "unnamed_code_count": len(result.unnamed_codes),
        "content_hash": result.content_hash,
        "asof": None,
        "labels": ["STATIC_ASOF_UNKNOWN"],
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-dir", type=Path, required=True)
    parser.add_argument("--dictionary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    build(args.csv_dir, args.dictionary, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
