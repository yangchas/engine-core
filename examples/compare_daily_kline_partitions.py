#!/usr/bin/env python3
"""Prove a daily_kline partition is column-for-column compatible with a reference partition (C579).

``left`` is the reference (e.g. a live partition in core-data), ``right`` the candidate (new chain output).
Checks, all fail-closed:
  * the candidate carries every reference column (columns no consumer reads are waived by name and reported);
  * the candidate's rows have the canonical columns and its manifest.field_units equal the canonical units;
  * per-column value types agree (null ignored; int and float are both "number");
  * unit sense, measured on the rows of each side: pct_chg is a percent (|pct - (close/pre_close-1)*100|),
    volume is shares (amount/volume lies inside [low, high]); a reference whose volume is in hands is detected
    and reported (the candidate must still be shares) and compared after x100;
  * values, symbol by symbol, within tolerance; mismatch counts are reported per column.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import statistics
from pathlib import Path
from typing import Any, Mapping

from engine_core.data_pipeline.daily_kline_schema import (
    CANONICAL_FIELD_UNITS,
    CANONICAL_ROW_FIELDS,
)

# Columns present in some real partitions that no consumer reads (docs/DAILY_KLINE.md, "Consumer evidence"):
# `turnover` duplicates turnover_pct, `mismatch`/`universe_version` are writer bookkeeping,
# `available_at`/`open_missing`/`quality` are row-level flags superseded by manifest fields / nulls.
WAIVED_REFERENCE_COLUMNS = ("turnover", "mismatch", "universe_version", "available_at", "open_missing",
                            "quality", "field_sources")
CODE_PATTERN = re.compile(r"^(sh|sz|bj)\.\d{6}$")  # live partitions: sz.000001; symbol is the bare 6 digits
PRICE_FIELDS = ("open", "high", "low", "close", "pre_close")
SENSE_MIN_FRACTION = 0.99


class ComparisonError(ValueError):
    pass


def _rows(path: Path) -> list[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _manifest_rows(manifest_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest, _rows(Path(manifest["rows_uri"]))


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def _row_types(rows: list[dict[str, Any]]) -> dict[str, str]:
    columns: dict[str, set[str]] = {}
    for row in rows:
        for key, value in row.items():
            columns.setdefault(key, set())
            if value is not None:
                columns[key].add(_kind(value))
    return {key: ("NULL" if not kinds else "MIXED:" + ",".join(sorted(kinds)) if len(kinds) > 1
                  else next(iter(kinds))) for key, kinds in sorted(columns.items())}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def unit_sense(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Measure the pct_chg and volume units from the rows themselves."""
    pct_ok = pct_n = vol_ok = vol_hands_ok = vol_n = 0
    for row in rows:
        close, pre, pct = _num(row.get("close")), _num(row.get("pre_close")), _num(row.get("pct_chg"))
        if close is not None and pre and pct is not None:
            pct_n += 1
            pct_ok += abs(pct - (close / pre - 1.0) * 100.0) <= 0.02
        amount, volume = _num(row.get("amount_yuan")), _num(row.get("volume"))
        low, high = _num(row.get("low")), _num(row.get("high"))
        if amount and volume and low is not None and high is not None:
            vol_n += 1
            vol_ok += low * 0.99 <= amount / volume <= high * 1.01
            vol_hands_ok += low * 0.99 <= amount / (volume * 100.0) <= high * 1.01
    pct_fraction = pct_ok / pct_n if pct_n else None
    shares_fraction = vol_ok / vol_n if vol_n else None
    hands_fraction = vol_hands_ok / vol_n if vol_n else None
    if shares_fraction is None:
        volume_unit = "UNDETERMINED"
    elif shares_fraction >= SENSE_MIN_FRACTION - 0.04:
        volume_unit = "shares"
    elif hands_fraction is not None and hands_fraction >= SENSE_MIN_FRACTION - 0.04:
        volume_unit = "hands"
    else:
        volume_unit = "UNKNOWN"
    return {"pct_chg_percent_fraction": pct_fraction, "pct_chg_rows": pct_n, "volume_unit": volume_unit,
            "volume_shares_fraction": shares_fraction, "volume_rows": vol_n}


def _unit_problems(side: str, manifest: Mapping[str, Any], sense: Mapping[str, Any],
                   *, candidate: bool) -> list[str]:
    errors: list[str] = []
    units = manifest.get("field_units", {})
    if candidate:
        if units != CANONICAL_FIELD_UNITS:
            errors.append(f"{side}_field_units_invalid")
    else:
        for key, value in (units or {}).items():
            canonical = CANONICAL_FIELD_UNITS.get(key)
            if canonical and value not in (canonical, "UNKNOWN", "CNY") and key != "price":
                errors.append(f"{side}_field_units_invalid")
                break
    fraction = sense.get("pct_chg_percent_fraction")
    if fraction is not None and fraction < SENSE_MIN_FRACTION:
        errors.append(f"{side}_pct_chg_not_percent")
    if candidate and sense.get("volume_unit") not in ("shares", "UNDETERMINED"):
        errors.append(f"{side}_volume_not_shares")
    return errors


def compare_partitions(left_manifest: Mapping[str, Any], left_rows: list[dict[str, Any]],
                       right_manifest: Mapping[str, Any], right_rows: list[dict[str, Any]], *,
                       max_mismatch_ratio: float = 0.0, skip_fields: tuple[str, ...] = (),
                       waived: tuple[str, ...] = WAIVED_REFERENCE_COLUMNS) -> dict[str, Any]:
    """Fail closed on column/type/unit drift, then compare values symbol by symbol."""
    errors: list[str] = []
    left_fields = set().union(*(set(row) for row in left_rows)) if left_rows else set()
    right_fields = set().union(*(set(row) for row in right_rows)) if right_rows else set(CANONICAL_ROW_FIELDS)
    missing_canonical = sorted(set(CANONICAL_ROW_FIELDS) - right_fields)
    if missing_canonical:
        errors.append("right_field_set_mismatch:" + "+".join(missing_canonical))
    waived_missing = sorted((left_fields - right_fields) & set(waived))
    missing = sorted(left_fields - right_fields - set(waived))
    if missing:
        errors.append("right_missing_columns:" + "+".join(missing))
    left_types, right_types = _row_types(left_rows), _row_types(right_rows)
    type_diff = {col: (left_types[col], right_types[col]) for col in left_types.keys() & right_types.keys()
                 if "NULL" not in (left_types[col], right_types[col]) and left_types[col] != right_types[col]}
    if type_diff:
        errors.append("field_type_difference:" + "+".join(sorted(type_diff)))
    for side, rows in (("left", left_rows), ("right", right_rows)):
        codes = [row.get("code") for row in rows if row.get("code") is not None]
        bad_code = [c for c in codes if not CODE_PATTERN.match(str(c))]
        if bad_code or any(row.get("symbol") is None or not str(row["symbol"]).isdigit()
                           or len(str(row["symbol"])) != 6 for row in rows):
            errors.append(f"{side}_code_or_symbol_format")
    left_sense, right_sense = unit_sense(left_rows), unit_sense(right_rows)
    errors += _unit_problems("left", left_manifest, left_sense, candidate=False)
    errors += _unit_problems("right", right_manifest, right_sense, candidate=True)
    if errors:
        raise ComparisonError(",".join(errors))

    volume_scale = 100.0 if left_sense["volume_unit"] == "hands" else 1.0
    left = {str(row["symbol"]): row for row in left_rows}
    right = {str(row["symbol"]): row for row in right_rows}
    common = sorted(left.keys() & right.keys())

    def tolerance(field: str, a: float, b: float) -> float:
        if field in PRICE_FIELDS:
            return max(0.011, abs(a) * 0.002)
        if field == "pct_chg":
            return 0.02
        if field == "turnover_pct":
            return 0.02
        return max(abs(a), abs(b)) * 1e-3 + 1.0  # amount / volume

    columns = [f for f in (*PRICE_FIELDS, "pct_chg", "turnover_pct", "amount_yuan", "volume")
               if f in left_fields and f in right_fields and f not in skip_fields]
    metrics: dict[str, dict[str, Any]] = {}
    for field in columns:
        compared = mismatched = missing_one_side = null_vs_value = 0
        for symbol in common:
            a, b = _num(left[symbol].get(field)), _num(right[symbol].get(field))
            if a is None or b is None:
                missing_one_side += 1
                null_vs_value += (a is None) != (b is None)
                continue
            if field == "volume":
                a *= volume_scale
            compared += 1
            mismatched += not math.isclose(a, b, rel_tol=0.0, abs_tol=tolerance(field, a, b))
        metrics[field] = {"compared": compared, "missing": missing_one_side,
                          "null_on_one_side_only": null_vs_value, "mismatched": mismatched}
    total_compared = sum(m["compared"] for m in metrics.values())
    total_mismatched = sum(m["mismatched"] for m in metrics.values())
    status = "PASS"
    if total_compared == 0 or total_mismatched > max_mismatch_ratio * total_compared:
        status = "FAIL_VALUES"

    return {
        "status": status,
        "left": {"provider": left_manifest.get("provider"), "source": left_manifest.get("source"),
                 "row_count": len(left_rows), "types": left_types, "unit_sense": left_sense},
        "right": {"provider": right_manifest.get("provider"), "source": right_manifest.get("source"),
                  "row_count": len(right_rows), "types": right_types, "unit_sense": right_sense},
        "canonical_field_units": dict(CANONICAL_FIELD_UNITS),
        "left_volume_scale_to_shares": volume_scale,
        "columns_only_in_left_waived": waived_missing,
        "columns_only_in_right": sorted(right_fields - left_fields),
        "common_symbols": len(common),
        "left_only": len(left.keys() - right.keys()),
        "right_only": len(right.keys() - left.keys()),
        "row_date_mismatches": sum(left[symbol].get("date") != right[symbol].get("date") for symbol in common),
        "value_comparison": metrics,
        "skipped_value_fields": list(skip_fields),
        "total_compared": total_compared,
        "total_mismatched": total_mismatched,
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
    parser.add_argument("--root", help="artifact root containing catalog.json (both sides)")
    parser.add_argument("--date", help="ISO trade date")
    parser.add_argument("--left", help="provider on the left when --root is used, or a manifest.json path")
    parser.add_argument("--right", help="provider on the right when --root is used, or a manifest.json path")
    parser.add_argument("--left-root", help="catalog root of the left side (default --root)")
    parser.add_argument("--right-root", help="catalog root of the right side (default --root)")
    parser.add_argument("--max-mismatch-ratio", type=float, default=0.0)
    parser.add_argument("--skip-fields", default="", help="comma list of value fields to leave out of the value "
                        "comparison (reported in the output; structure/unit checks still cover them)")
    args = parser.parse_args(argv)
    try:
        sides = []
        for name, root_arg in (("left", args.left_root or args.root), ("right", args.right_root or args.root)):
            ref = getattr(args, name)
            if ref and ref.endswith(".json"):
                sides.append(Path(ref))
            else:
                if not (root_arg and args.date and ref):
                    raise ValueError(f"--{name} needs --date and a root, or a manifest.json path")
                sides.append(_find_manifest(Path(root_arg).expanduser(), args.date, ref))
        left_manifest, left_rows = _manifest_rows(sides[0])
        right_manifest, right_rows = _manifest_rows(sides[1])
        result = compare_partitions(left_manifest, left_rows, right_manifest, right_rows,
                                    max_mismatch_ratio=args.max_mismatch_ratio,
                                    skip_fields=tuple(f for f in args.skip_fields.split(",") if f))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0 if result["status"] == "PASS" else 1
    except (ComparisonError, FileNotFoundError, KeyError, OSError, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc)}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
