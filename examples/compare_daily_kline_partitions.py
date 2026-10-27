#!/usr/bin/env python3
"""Compare daily-Kline artifact structure, units, and selected source values."""
from __future__ import annotations

import argparse
import gzip
import json
import math
from pathlib import Path
from typing import Any, Mapping

from engine_core.data_pipeline.daily_kline_schema import (
    CANONICAL_FIELD_UNITS,
    CANONICAL_ROW_FIELDS,
)


class ComparisonError(ValueError):
    pass


def _rows(path: Path) -> list[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _manifest_rows(manifest_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest, _rows(Path(manifest["rows_uri"]))


def _row_types(rows: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for field in CANONICAL_ROW_FIELDS:
        values = {type(row[field]).__name__ for row in rows if field in row and row[field] is not None}
        if len(values) > 1:
            result[field] = "MIXED:" + ",".join(sorted(values))
        elif values:
            result[field] = next(iter(values))
        else:
            result[field] = "NULL"
    return result


def compare_partitions(left_manifest: Mapping[str, Any], left_rows: list[dict[str, Any]],
                       right_manifest: Mapping[str, Any], right_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fail closed on field/type/unit drift, then compare agreed canonical values."""
    errors: list[str] = []
    left_fields = set(left_rows[0]) if left_rows else set(CANONICAL_ROW_FIELDS)
    right_fields = set(right_rows[0]) if right_rows else set(CANONICAL_ROW_FIELDS)
    if left_fields != set(CANONICAL_ROW_FIELDS):
        errors.append("left_field_set_mismatch")
    if right_fields != set(CANONICAL_ROW_FIELDS):
        errors.append("right_field_set_mismatch")
    if left_fields != right_fields:
        errors.append("field_set_difference")
    left_types, right_types = _row_types(left_rows), _row_types(right_rows)
    if left_types != right_types:
        errors.append("field_type_difference")
    left_units = left_manifest.get("field_units", {})
    right_units = right_manifest.get("field_units", {})
    if left_units != CANONICAL_FIELD_UNITS:
        errors.append("left_field_units_invalid")
    if right_units != CANONICAL_FIELD_UNITS:
        errors.append("right_field_units_invalid")
    if left_units != right_units:
        errors.append("field_unit_difference")
    if errors:
        raise ComparisonError(",".join(errors))

    left = {str(row["symbol"]): row for row in left_rows}
    right = {str(row["symbol"]): row for row in right_rows}
    common = sorted(left.keys() & right.keys())
    metrics: dict[str, dict[str, int]] = {}
    for field in ("open", "high", "low", "close", "pct_chg"):
        compared = mismatched = missing = 0
        for symbol in common:
            a, b = left[symbol].get(field), right[symbol].get(field)
            if a is None or b is None:
                missing += 1
                continue
            compared += 1
            if field == "pct_chg":
                a, b = a / 100.0, b / 100.0
            tolerance = max(0.011, abs(float(a)) * 0.002)
            if not math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=tolerance):
                mismatched += 1
        metrics[field] = {"compared": compared, "missing": missing, "mismatched": mismatched}

    return {
        "status": "PASS",
        "left": {"provider": left_manifest.get("provider"), "source": left_manifest.get("source"),
                 "row_count": len(left_rows), "types": left_types},
        "right": {"provider": right_manifest.get("provider"), "source": right_manifest.get("source"),
                  "row_count": len(right_rows), "types": right_types},
        "canonical_field_units": dict(CANONICAL_FIELD_UNITS),
        "common_symbols": len(common),
        "left_only": len(left.keys() - right.keys()),
        "right_only": len(right.keys() - left.keys()),
        "row_date_mismatches": sum(left[symbol].get("date") != right[symbol].get("date")
                                    for symbol in common),
        "value_comparison": metrics,
        "left_source_dates": left_manifest.get("source_dates", {}),
        "right_source_dates": right_manifest.get("source_dates", {}),
    }


def _find_manifest(root: Path, trade_date: str, provider: str) -> Path:
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    versions = catalog.get("datasets", {}).get("daily_kline", {}).get("dates", {}).get(trade_date, [])
    aliases = {"q2_final": {"q2_final", "legacy_q2_final"}}
    accepted = aliases.get(provider, {provider})
    for version in reversed(versions):
        manifest = Path(version["manifest"])
        try:
            candidate = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if candidate.get("provider") == provider or candidate.get("source") in accepted:
            return manifest
    raise FileNotFoundError(f"daily_kline partition not found: {trade_date} {provider}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="artifact root containing catalog.json")
    parser.add_argument("--date", required=True, help="ISO trade date")
    parser.add_argument("--left", required=True, help="baostock, wencai, or q2_final")
    parser.add_argument("--right", required=True, help="baostock, wencai, or q2_final")
    args = parser.parse_args(argv)
    try:
        root = Path(args.root).expanduser()
        left_manifest_path = _find_manifest(root, args.date, args.left)
        right_manifest_path = _find_manifest(root, args.date, args.right)
        left_manifest, left_rows = _manifest_rows(left_manifest_path)
        right_manifest, right_rows = _manifest_rows(right_manifest_path)
        result = compare_partitions(left_manifest, left_rows, right_manifest, right_rows)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (ComparisonError, FileNotFoundError, KeyError, OSError, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc)}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
