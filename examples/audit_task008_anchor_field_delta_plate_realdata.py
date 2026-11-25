"""Audit field-level plate aggregation against pinned real TD and mapping data.

This is a file-only audit over sealed TD SELECT rows and a frozen mapping
snapshot. It independently recomputes per-field deltas from those raw rows,
then compares them with the Core plate summary. No live data source is opened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core import (  # noqa: E402
    build_anchor_field_delta_evidence,
    build_opening_plate_field_delta_summary,
)
from engine_core.contracts import semantic_hash  # noqa: E402


DEFAULT_INPUT_DIR = Path(
    "/home/exedev/validation/task008-opening-plate-amount-integrated-"
    "20261001T1142+0800"
)
DEFAULT_TD_SHA256 = "b66b78971f6453ad0ab48e5fc818ce7b1e9799afc03bf3e2e9adffae7d44332b"
DEFAULT_MAPPING_SHA256 = "c88eb9339fb1a30dbf6c82da41eebca12f8128a399bd045eaa3da4fbf1a4553b"
DEFAULT_TRADE_DATE = "2026-09-29"
FIELD_NAMES = (
    "amount_yuan",
    "rest_bid_yuan",
    "rest_ask_yuan",
    "book_pressure_yuan",
)
RAW_FIELDS = {
    "amount_yuan": "match_amt_yuan",
    "rest_bid_yuan": "rest_bid_amt_yuan",
    "rest_ask_yuan": "rest_ask_amt_yuan",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise ValueError(f"expected JSON object on line {line_number}")
            rows.append(dict(value))
    return rows


def _raw_value(row: Mapping[str, Any] | None, name: str) -> tuple[float | None, str]:
    if row is None:
        return None, "MISSING"
    if name not in row:
        return None, "UNKNOWN"
    raw = row[name]
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, "MISSING"
    if isinstance(raw, bool):
        return None, "INVALID"
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, "INVALID"
    if not math.isfinite(value) or value < 0:
        return None, "INVALID"
    return value, "AVAILABLE"


def _combined_status(statuses: tuple[str, ...]) -> str:
    for status in ("INVALID", "UNKNOWN", "MISSING"):
        if status in statuses:
            return status
    return "AVAILABLE"


def _raw_deltas(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None
) -> dict[str, tuple[float | None, str]]:
    values: dict[str, tuple[float | None, str]] = {}
    for field_name, raw_name in RAW_FIELDS.items():
        old_value, old_status = _raw_value(before, raw_name)
        new_value, new_status = _raw_value(after, raw_name)
        status = _combined_status((old_status, new_status))
        values[field_name] = (
            (new_value - old_value, "AVAILABLE")
            if status == "AVAILABLE"
            else (None, status)
        )
    bid, bid_status = values["rest_bid_yuan"]
    ask, ask_status = values["rest_ask_yuan"]
    pressure_status = _combined_status((bid_status, ask_status))
    values["book_pressure_yuan"] = (
        (bid - ask, "AVAILABLE")
        if pressure_status == "AVAILABLE"
        else (None, pressure_status)
    )
    return values


def _independent_plate_summary(
    rows_by_tag_symbol: Mapping[tuple[str, str], Mapping[str, Any]],
    mapped_symbols_by_plate: Mapping[str, set[str]],
) -> dict[str, dict[str, dict[str, Any]]]:
    expected: dict[str, dict[str, dict[str, Any]]] = {}
    for plate, members in sorted(mapped_symbols_by_plate.items()):
        expected[plate] = {}
        for field_name in FIELD_NAMES:
            available: list[float] = []
            observed = missing = unknown = invalid = missing_symbol = zero = 0
            for symbol in sorted(members):
                before = rows_by_tag_symbol.get(("0924", symbol))
                after = rows_by_tag_symbol.get(("0925", symbol))
                if before is None and after is None:
                    missing_symbol += 1
                    continue
                observed += 1
                value, status = _raw_deltas(before, after)[field_name]
                if status == "AVAILABLE":
                    assert value is not None
                    available.append(value)
                    zero += value == 0
                elif status == "MISSING":
                    missing += 1
                elif status == "UNKNOWN":
                    unknown += 1
                else:
                    invalid += 1
            total = len(members)
            usable = len(available)
            expected[plate][field_name] = {
                "expected_count": total,
                "observed_count": observed,
                "available_count": usable,
                "missing_count": missing,
                "unknown_count": unknown,
                "invalid_count": invalid,
                "missing_symbol_count": missing_symbol,
                "coverage": usable / total if total else None,
                "zero_value_count": zero,
                "sum_yuan": math.fsum(available) if available else None,
                "status": (
                    "available"
                    if total and usable == total
                    else "partial"
                    if usable
                    else "unavailable"
                ),
            }
    return expected


def audit(
    input_dir: Path,
    *,
    verify_pinned_hashes: bool = True,
    td_rows_path: Path | None = None,
    mapping_path: Path | None = None,
    trade_date: str = DEFAULT_TRADE_DATE,
    expected_td_sha256: str | None = None,
    expected_mapping_sha256: str | None = None,
) -> dict[str, Any]:
    td_path = td_rows_path or input_dir / "td_auction_snapshot_rows.jsonl"
    mapping_path = mapping_path or input_dir / "stock_plate_snapshot.json"
    td_hash = _sha256(td_path)
    mapping_hash = _sha256(mapping_path)
    if expected_td_sha256 is None and td_path.parent == DEFAULT_INPUT_DIR:
        expected_td_sha256 = DEFAULT_TD_SHA256
    if expected_mapping_sha256 is None and mapping_path.parent == DEFAULT_INPUT_DIR:
        expected_mapping_sha256 = DEFAULT_MAPPING_SHA256
    if verify_pinned_hashes and expected_td_sha256 is not None and td_hash != expected_td_sha256:
        raise ValueError("TD capture SHA-256 does not match the pinned artifact")
    if verify_pinned_hashes and expected_mapping_sha256 is not None and mapping_hash != expected_mapping_sha256:
        raise ValueError("plate mapping SHA-256 does not match the pinned artifact")

    mapping_snapshot = json.loads(mapping_path.read_text(encoding="utf-8"))
    if mapping_snapshot.get("trade_date") != trade_date:
        raise ValueError("plate mapping is not from the requested trade date")
    symbol_to_plate = mapping_snapshot.get("mapping")
    if not isinstance(symbol_to_plate, Mapping):
        raise ValueError("plate mapping snapshot has no mapping object")
    mapped_symbols_by_plate: dict[str, set[str]] = {}
    for symbol, plate in symbol_to_plate.items():
        mapped_symbols_by_plate.setdefault(str(plate), set()).add(str(symbol))

    rows_by_tag_symbol: dict[tuple[str, str], dict[str, Any]] = {}
    row_counts: Counter[str] = Counter()
    for row in _read_rows(td_path):
        tag = str(row.get("auction_tag") or "")
        if tag not in {"0924", "0925"}:
            continue
        if row.get("trade_date") != trade_date.replace("-", ""):
            raise ValueError("TD capture contains a row from another trade date")
        symbol = str(row.get("symbol") or "").strip()
        key = (tag, symbol)
        if not symbol or key in rows_by_tag_symbol:
            raise ValueError(f"empty symbol or duplicate TD anchor row: {key}")
        rows_by_tag_symbol[key] = row
        row_counts[tag] += 1

    symbols_by_tag = {
        tag: {symbol for row_tag, symbol in rows_by_tag_symbol if row_tag == tag}
        for tag in ("0924", "0925")
    }
    captured_symbols = set().union(*symbols_by_tag.values())
    mapped_symbols = set(symbol_to_plate)
    symbols = sorted({symbol for _, symbol in rows_by_tag_symbol})
    field_facts = {
        symbol: build_anchor_field_delta_evidence(
            rows_by_tag_symbol.get(("0924", symbol)),
            rows_by_tag_symbol.get(("0925", symbol)),
            symbol=symbol,
        )
        for symbol in symbols
    }
    summary = build_opening_plate_field_delta_summary(
        field_facts,
        mapped_symbols_by_plate={
            plate: tuple(sorted(members))
            for plate, members in mapped_symbols_by_plate.items()
        },
        trade_date=trade_date,
    )
    independent = _independent_plate_summary(rows_by_tag_symbol, mapped_symbols_by_plate)

    mismatches: list[str] = []
    field_totals = {
        name: {
            "expected_count": 0,
            "observed_count": 0,
            "available_count": 0,
            "missing_count": 0,
            "unknown_count": 0,
            "invalid_count": 0,
            "missing_symbol_count": 0,
            "zero_value_count": 0,
            "sum_yuan": 0.0,
        }
        for name in FIELD_NAMES
    }
    for plate_row in summary["plates"]:
        plate = plate_row["plate"]
        for field_name in FIELD_NAMES:
            actual = plate_row["fields"][field_name]
            expected = independent[plate][field_name]
            for key in expected:
                left, right = actual[key], expected[key]
                if key == "sum_yuan" and left is not None and right is not None:
                    equal = math.isclose(left, right, rel_tol=0, abs_tol=1e-9)
                else:
                    equal = left == right
                if not equal:
                    mismatches.append(f"{plate}/{field_name}/{key}")
            totals = field_totals[field_name]
            for key in (
                "expected_count",
                "observed_count",
                "available_count",
                "missing_count",
                "unknown_count",
                "invalid_count",
                "missing_symbol_count",
                "zero_value_count",
            ):
                totals[key] += actual[key]
            if actual["sum_yuan"] is not None:
                totals["sum_yuan"] += actual["sum_yuan"]

    repeat_summary = build_opening_plate_field_delta_summary(
        dict(reversed(list(field_facts.items()))),
        mapped_symbols_by_plate={
            plate: tuple(reversed(sorted(members)))
            for plate, members in reversed(list(mapped_symbols_by_plate.items()))
        },
        trade_date=trade_date,
    )
    if repeat_summary["content_hash"] != summary["content_hash"]:
        mismatches.append("repeat/content_hash")

    result = {
        "contract": "OpeningPlateFieldDeltaRealDataAuditV1",
        "trade_date": trade_date,
        "td_capture_sha256": td_hash,
        "plate_mapping_sha256": mapping_hash,
        "source_row_counts": dict(sorted(row_counts.items())),
        "source_symbol_count": len(symbols),
        "mapped_symbol_count": len(symbol_to_plate),
        "mapping_capture_overlap_count": len(mapped_symbols & captured_symbols),
        "mapping_only_symbol_count": len(mapped_symbols - captured_symbols),
        "capture_only_symbol_count": len(captured_symbols - mapped_symbols),
        "mapped_both_anchor_symbol_count": len(
            mapped_symbols & symbols_by_tag["0924"] & symbols_by_tag["0925"]
        ),
        "plate_count": len(summary["plates"]),
        "summary_content_hash": summary["content_hash"],
        "field_totals": field_totals,
        "field_delta_status_counts": {
            field_name: dict(
                sorted(
                    Counter(
                        row["fields"][field_name]["status"]
                        for row in summary["plates"]
                    ).items()
                )
            )
            for field_name in FIELD_NAMES
        },
        "independent_raw_row_parity": "PASS" if not mismatches else "FAIL",
        "parity_mismatch_count": len(mismatches),
        "parity_mismatch_samples": mismatches[:20],
        "decision_status": summary["decision_status"],
        "full_market_coverage": summary["full_market_coverage"],
        "side_effects": "NONE_OBSERVED",
        "limits": [
            "This uses sealed same-date TD SELECT output and frozen mapping, not a fresh source read.",
            "Historical availability, live Redis visibility, and full-market completeness are not proven.",
            "Per-field sums are FACT_ONLY and are not direction, net-flow, or strategy claims.",
        ],
    }
    if mismatches:
        raise AssertionError(f"Core field aggregation differs from raw row recomputation: {mismatches[:5]}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--td-rows", type=Path)
    parser.add_argument("--mapping-snapshot", type=Path)
    parser.add_argument("--trade-date", default=DEFAULT_TRADE_DATE)
    parser.add_argument("--expected-td-sha256")
    parser.add_argument("--expected-mapping-sha256")
    parser.add_argument("--skip-pinned-hashes", action="store_true")
    args = parser.parse_args()
    custom_inputs = any(
        value is not None
        for value in (args.td_rows, args.mapping_snapshot)
    ) or args.trade_date != DEFAULT_TRADE_DATE
    if custom_inputs and not args.skip_pinned_hashes and (
        args.expected_td_sha256 is None or args.expected_mapping_sha256 is None
    ):
        parser.error("custom inputs require explicit expected TD and mapping SHA-256 values")
    print(
        json.dumps(
            audit(
                args.input_dir,
                verify_pinned_hashes=not args.skip_pinned_hashes,
                td_rows_path=args.td_rows,
                mapping_path=args.mapping_snapshot,
                trade_date=args.trade_date,
                expected_td_sha256=args.expected_td_sha256,
                expected_mapping_sha256=args.expected_mapping_sha256,
            ),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
