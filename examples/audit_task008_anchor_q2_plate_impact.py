"""Summarize sealed Q2/TD anchor candidate differences by frozen plate map.

This is a file-only sensitivity diagnostic. It does not invoke a production
assembler or claim that the event-time candidate was visible in Redis.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping


ANCHOR_TAGS = ("0920", "0924", "0925")
FIELD_NAMES = ("am", "br", "ar")
DEFAULT_TRADE_DATE = "2026-09-30"
DEFAULT_EVIDENCE = Path(
    "/home/exedev/validation/task008-anchor-q2-field-alignment-final-"
    "20260930-20261008T062642+0800/symbol_field_evidence.jsonl"
)
DEFAULT_MAPPING = Path(
    "/home/exedev/services/engine-next/shared/runtime_state/2026-09-30/"
    "stock_plate_snapshot.json"
)
EXPECTED_EVIDENCE_SHA256 = "f96cf37e0e0bff78060948aeea400f78559a1f21614d6e7cab98683b7dca67f9"
EXPECTED_MAPPING_SHA256 = "8dbf5a1d689d5d2acba09795133305ed28e3eedc7e8ebf0acc69261ac02fccbc"
COMPARISON_STATUSES = {
    "EQUAL",
    "DIFFERENT",
    "TD_NULL",
    "TD_INVALID",
    "NO_Q2_CANDIDATE",
    "Q2_NULL",
    "Q2_INVALID",
}
TD_FIELD_STATUSES = {"PRESENT", "NULL", "INVALID"}
Q2_FIELD_STATUSES = {"PRESENT", "NULL", "INVALID", "NO_CANDIDATE"}


def _compact_date(value: Any) -> str:
    return str(value or "").replace("-", "").strip()


def _new_field_stats() -> dict[str, Any]:
    return {
        "compared_count": 0,
        "equal_count": 0,
        "different_count": 0,
        "comparison_status_counts": Counter(),
        "td_value_sum": 0,
        "q2_value_sum": 0,
        "signed_difference_sum": 0,
        "gross_absolute_difference_sum": 0,
        "maximum_absolute_difference": 0,
        "maximum_difference_symbol": None,
    }


def _new_plate_stats() -> dict[str, Any]:
    return {"observed_symbols": set(), "fields": {name: _new_field_stats() for name in FIELD_NAMES}}


def _validate_pair(field_row: Mapping[str, Any], *, tag: str, symbol: str, name: str) -> tuple[str, int | None, int | None]:
    td_status = field_row.get("td_status")
    q2_status = field_row.get("q2_status")
    td_value = field_row.get("td_value")
    q2_value = field_row.get("q2_value")
    comparison = field_row.get("comparison")
    if td_status not in TD_FIELD_STATUSES:
        raise ValueError(f"unsupported TD field status at {tag}/{symbol}/{name}: {td_status!r}")
    if q2_status not in Q2_FIELD_STATUSES:
        raise ValueError(f"unsupported Q2 field status at {tag}/{symbol}/{name}: {q2_status!r}")
    if comparison not in COMPARISON_STATUSES:
        raise ValueError(f"unsupported comparison status at {tag}/{symbol}/{name}: {comparison!r}")

    if td_status == "PRESENT":
        if isinstance(td_value, bool) or not isinstance(td_value, int):
            raise ValueError(f"PRESENT TD value must be an integer at {tag}/{symbol}/{name}")
    elif td_value is not None:
        raise ValueError(f"non-PRESENT TD value must be null at {tag}/{symbol}/{name}")

    if q2_status == "PRESENT":
        if isinstance(q2_value, bool) or not isinstance(q2_value, int):
            raise ValueError(f"PRESENT Q2 value must be an integer at {tag}/{symbol}/{name}")
    elif q2_value is not None:
        raise ValueError(f"non-PRESENT Q2 value must be null at {tag}/{symbol}/{name}")

    if td_status == "NULL":
        expected = "TD_NULL"
    elif td_status == "INVALID":
        expected = "TD_INVALID"
    elif q2_status == "NO_CANDIDATE":
        expected = "NO_Q2_CANDIDATE"
    elif q2_status == "NULL":
        expected = "Q2_NULL"
    elif q2_status == "INVALID":
        expected = "Q2_INVALID"
    else:
        expected = "EQUAL" if td_value == q2_value else "DIFFERENT"

    if comparison != expected:
        raise ValueError(
            f"field status/comparison disagreement at {tag}/{symbol}/{name}: "
            f"expected {expected}, got {comparison}"
        )
    if expected in {"EQUAL", "DIFFERENT"}:
        return comparison, td_value, q2_value
    return comparison, None, None


def _accumulate(stats: dict[str, Any], field_row: Mapping[str, Any], *, tag: str, symbol: str, name: str) -> None:
    comparison, td_value, q2_value = _validate_pair(field_row, tag=tag, symbol=symbol, name=name)
    stats["comparison_status_counts"][comparison] += 1
    if comparison not in {"EQUAL", "DIFFERENT"}:
        return

    assert td_value is not None and q2_value is not None
    difference = q2_value - td_value
    absolute_difference = abs(difference)
    stats["compared_count"] += 1
    stats["td_value_sum"] += td_value
    stats["q2_value_sum"] += q2_value
    stats["signed_difference_sum"] += difference
    stats["gross_absolute_difference_sum"] += absolute_difference
    if comparison == "EQUAL":
        stats["equal_count"] += 1
    else:
        stats["different_count"] += 1
    if absolute_difference > stats["maximum_absolute_difference"]:
        stats["maximum_absolute_difference"] = absolute_difference
        stats["maximum_difference_symbol"] = symbol


def _finalize_field(stats: Mapping[str, Any]) -> dict[str, Any]:
    compared = int(stats["compared_count"])
    return {
        "compared_count": compared,
        "equal_count": int(stats["equal_count"]),
        "different_count": int(stats["different_count"]),
        "comparison_status_counts": dict(sorted(stats["comparison_status_counts"].items())),
        "td_value_sum": stats["td_value_sum"] if compared else None,
        "q2_value_sum": stats["q2_value_sum"] if compared else None,
        "signed_difference_sum": stats["signed_difference_sum"] if compared else None,
        "gross_absolute_difference_sum": stats["gross_absolute_difference_sum"] if compared else None,
        "maximum_absolute_difference": stats["maximum_absolute_difference"] if compared else None,
        "maximum_difference_symbol": stats["maximum_difference_symbol"],
    }


def summarize_plate_candidate_impact(
    symbol_evidence: Iterable[Mapping[str, Any]],
    mapping_snapshot: Mapping[str, Any],
    *,
    trade_date: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Aggregate pairwise-valid Q2/TD values over the frozen one-plate map.

    Unmapped captured symbols and map members absent from the captured TD
    cohort are reported separately. Null/invalid values are never changed to
    zero. All results remain source-time candidate diagnostics, not live plate
    facts.
    """

    expected_date = _compact_date(trade_date)
    if not re.fullmatch(r"\d{8}", expected_date):
        raise ValueError("trade_date must be YYYY-MM-DD or YYYYMMDD")
    if _compact_date(mapping_snapshot.get("trade_date")) != expected_date:
        raise ValueError("mapping trade_date does not match requested trade_date")
    symbol_to_plate = mapping_snapshot.get("mapping")
    if not isinstance(symbol_to_plate, Mapping):
        raise ValueError("mapping snapshot must contain a mapping object")

    members_by_plate: dict[str, set[str]] = {}
    for raw_symbol, raw_plate in symbol_to_plate.items():
        symbol = str(raw_symbol).strip()
        plate = str(raw_plate).strip() if raw_plate is not None else ""
        if not re.fullmatch(r"\d{6}", symbol) or not plate:
            raise ValueError(f"invalid symbol/plate in mapping: {raw_symbol!r}/{raw_plate!r}")
        members_by_plate.setdefault(plate, set()).add(symbol)
    symbol_to_plate_normalized = {
        symbol: plate for plate, symbols in members_by_plate.items() for symbol in symbols
    }

    state_by_tag: dict[str, dict[str, Any]] = {}
    for tag in ANCHOR_TAGS:
        state_by_tag[tag] = {
            "observed_symbols": set(),
            "unmapped_symbols": set(),
            "mapped_symbols": set(),
            "timestamps_ms": set(),
            "fields": {name: _new_field_stats() for name in FIELD_NAMES},
            "plates": {plate: _new_plate_stats() for plate in members_by_plate},
        }

    seen: set[tuple[str, str]] = set()
    evidence_rows = 0
    for row in symbol_evidence:
        if not isinstance(row, Mapping):
            raise ValueError("symbol evidence rows must be objects")
        tag = str(row.get("auction_tag") or "").strip()
        symbol = str(row.get("symbol") or "").strip()
        if tag not in state_by_tag:
            raise ValueError(f"unsupported anchor tag: {tag!r}")
        if not re.fullmatch(r"\d{6}", symbol):
            raise ValueError(f"invalid evidence symbol: {symbol!r}")
        key = (tag, symbol)
        if key in seen:
            raise ValueError(f"duplicate anchor symbol: {tag}/{symbol}")
        seen.add(key)
        evidence_rows += 1

        tag_state = state_by_tag[tag]
        tag_state["observed_symbols"].add(symbol)
        timestamp_ms = row.get("td_timestamp_ms")
        if isinstance(timestamp_ms, int) and not isinstance(timestamp_ms, bool):
            tag_state["timestamps_ms"].add(timestamp_ms)

        fields = row.get("fields")
        if not isinstance(fields, Mapping):
            raise ValueError(f"missing fields object at {tag}/{symbol}")
        if any(name not in fields or not isinstance(fields[name], Mapping) for name in FIELD_NAMES):
            raise ValueError(f"incomplete field evidence at {tag}/{symbol}")

        plate = symbol_to_plate_normalized.get(symbol)
        if plate is None:
            tag_state["unmapped_symbols"].add(symbol)
            continue

        tag_state["mapped_symbols"].add(symbol)
        plate_state = tag_state["plates"][plate]
        plate_state["observed_symbols"].add(symbol)
        for name in FIELD_NAMES:
            _accumulate(tag_state["fields"][name], fields[name], tag=tag, symbol=symbol, name=name)
            _accumulate(plate_state["fields"][name], fields[name], tag=tag, symbol=symbol, name=name)

    plate_rows: list[dict[str, Any]] = []
    tag_reports: dict[str, Any] = {}
    for tag in ANCHOR_TAGS:
        tag_state = state_by_tag[tag]
        mapped_symbols = tag_state["mapped_symbols"]
        tag_plate_rows: list[dict[str, Any]] = []
        for plate in sorted(members_by_plate):
            members = members_by_plate[plate]
            plate_state = tag_state["plates"][plate]
            observed_members = plate_state["observed_symbols"]
            plate_row = {
                "trade_date": expected_date,
                "auction_tag": tag,
                "plate": plate,
                "mapping_member_count": len(members),
                "observed_member_count": len(observed_members),
                "mapping_members_not_observed_count": len(members - observed_members),
                "fields": {
                    name: _finalize_field(plate_state["fields"][name])
                    for name in FIELD_NAMES
                },
            }
            tag_plate_rows.append(plate_row)
            plate_rows.append(plate_row)

        top_by_field: dict[str, list[dict[str, Any]]] = {}
        for name in FIELD_NAMES:
            ranked = sorted(
                tag_plate_rows,
                key=lambda item: (
                    -(item["fields"][name]["gross_absolute_difference_sum"] or 0),
                    item["plate"],
                ),
            )
            top_by_field[name] = [
                {
                    "plate": item["plate"],
                    "observed_member_count": item["observed_member_count"],
                    "compared_count": item["fields"][name]["compared_count"],
                    "gross_absolute_difference_sum": item["fields"][name]["gross_absolute_difference_sum"],
                    "signed_difference_sum": item["fields"][name]["signed_difference_sum"],
                }
                for item in ranked[:10]
                if item["fields"][name]["compared_count"]
            ]

        partition_reconciliation: dict[str, str] = {}
        for name in FIELD_NAMES:
            total = _finalize_field(tag_state["fields"][name])
            plate_fields = [row["fields"][name] for row in tag_plate_rows]
            child_status_counts: Counter[str] = Counter()
            for row in plate_fields:
                child_status_counts.update(row["comparison_status_counts"])
            child_compared = sum(row["compared_count"] for row in plate_fields)
            child_equal = sum(row["equal_count"] for row in plate_fields)
            child_different = sum(row["different_count"] for row in plate_fields)
            child_td_sum = sum(row["td_value_sum"] or 0 for row in plate_fields)
            child_q2_sum = sum(row["q2_value_sum"] or 0 for row in plate_fields)
            child_signed = sum(row["signed_difference_sum"] or 0 for row in plate_fields)
            child_gross = sum(row["gross_absolute_difference_sum"] or 0 for row in plate_fields)
            child_max = max(
                (row["maximum_absolute_difference"] or 0 for row in plate_fields),
                default=0,
            )
            matched = (
                child_compared == total["compared_count"]
                and child_equal == total["equal_count"]
                and child_different == total["different_count"]
                and child_status_counts == Counter(total["comparison_status_counts"])
                and (child_td_sum if child_compared else None) == total["td_value_sum"]
                and (child_q2_sum if child_compared else None) == total["q2_value_sum"]
                and (child_signed if child_compared else None) == total["signed_difference_sum"]
                and (child_gross if child_compared else None) == total["gross_absolute_difference_sum"]
                and (child_max if child_compared else None) == total["maximum_absolute_difference"]
            )
            partition_reconciliation[name] = "PASS" if matched else "MISMATCH"
            if not matched:
                raise ValueError(f"plate partition does not reconcile for {tag}/{name}")

        mapped_count = len(mapped_symbols)
        observed_count = len(tag_state["observed_symbols"])
        tag_reports[tag] = {
            "td_snapshot_timestamp_range_ms": {
                "min": min(tag_state["timestamps_ms"]) if tag_state["timestamps_ms"] else None,
                "max": max(tag_state["timestamps_ms"]) if tag_state["timestamps_ms"] else None,
                "unique_timestamp_count": len(tag_state["timestamps_ms"]),
            },
            "observed_td_rows": observed_count,
            "mapped_td_rows": mapped_count,
            "unmapped_td_rows": len(tag_state["unmapped_symbols"]),
            "unmapped_symbols": sorted(tag_state["unmapped_symbols"]),
            "mapping_members_not_observed_count": len(set(symbol_to_plate_normalized) - tag_state["observed_symbols"]),
            "mapping_coverage_of_observed_rows": mapped_count / observed_count if observed_count else None,
            "fields": {
                name: _finalize_field(tag_state["fields"][name])
                for name in FIELD_NAMES
            },
            "plate_partition_reconciliation": partition_reconciliation,
            "largest_plate_gross_differences": top_by_field,
            "scope": "frozen one-plate map intersected with observed TD anchor cohort; not production plate output",
        }

    summary = {
        "status": "OBSERVED_DIAGNOSTIC_NOT_A_GATE",
        "trade_date": expected_date,
        "mapping_effective_time": mapping_snapshot.get("effective_time"),
        "mapping_member_count": len(symbol_to_plate_normalized),
        "mapping_plate_count": len(members_by_plate),
        "symbol_evidence_rows": evidence_rows,
        "tags": tag_reports,
        "source_semantics": {
            "q2_input": "sealed event-time Q2Frame-derived per-symbol candidate values",
            "mapping": "same-date frozen engine-next runtime stock-to-one-plate snapshot",
            "rabbit_arrival_order": "UNKNOWN",
            "historical_redis_visibility": "UNKNOWN",
            "historical_available_at": "UNKNOWN",
            "full_market_coverage": "UNPROVEN",
            "production_plate_assembler_result": "not invoked; this is a sensitivity diagnostic only",
        },
        "missing_value_policy": "Only PRESENT/PRESENT pairs contribute to sums; NULL/INVALID/unmapped values are counted or listed, never replaced with zero.",
        "side_effects": "NONE_OBSERVED; sealed files only",
    }
    return summary, plate_rows


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}") from exc
            if not isinstance(value, Mapping):
                raise ValueError(f"expected JSON object at {path}:{line_number}")
            yield dict(value)


def run_audit(
    *,
    symbol_evidence_path: Path,
    mapping_path: Path,
    output_dir: Path,
    trade_date: str,
    expected_evidence_sha256: str | None = EXPECTED_EVIDENCE_SHA256,
    expected_mapping_sha256: str | None = EXPECTED_MAPPING_SHA256,
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    if not symbol_evidence_path.is_file() or not mapping_path.is_file():
        raise FileNotFoundError("symbol evidence and mapping snapshot must exist")
    evidence_sha256 = _sha256(symbol_evidence_path)
    mapping_sha256 = _sha256(mapping_path)
    if expected_evidence_sha256 and evidence_sha256 != expected_evidence_sha256:
        raise ValueError("symbol evidence SHA-256 does not match pinned input")
    if expected_mapping_sha256 and mapping_sha256 != expected_mapping_sha256:
        raise ValueError("mapping snapshot SHA-256 does not match pinned input")

    with mapping_path.open("r", encoding="utf-8") as handle:
        mapping_snapshot = json.load(handle)
    if not isinstance(mapping_snapshot, Mapping):
        raise ValueError("mapping snapshot must be a JSON object")

    summary, plate_rows = summarize_plate_candidate_impact(
        _iter_jsonl(symbol_evidence_path), mapping_snapshot, trade_date=trade_date
    )
    summary["inputs"] = {
        "symbol_evidence": {"path": str(symbol_evidence_path), "sha256": evidence_sha256},
        "mapping_snapshot": {"path": str(mapping_path), "sha256": mapping_sha256},
    }

    output_dir.mkdir(parents=True, exist_ok=False)
    summary_path = output_dir / "plate_impact_summary.json"
    evidence_path = output_dir / "plate_field_impact.jsonl"
    with summary_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    with evidence_path.open("x", encoding="utf-8") as handle:
        for row in plate_rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    sums_path = output_dir / "sha256sums.txt"
    with sums_path.open("x", encoding="utf-8") as handle:
        for path in (summary_path, evidence_path):
            handle.write(f"{_sha256(path)}  {path.name}\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", default=DEFAULT_TRADE_DATE)
    parser.add_argument("--symbol-evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--mapping-snapshot", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = run_audit(
        symbol_evidence_path=args.symbol_evidence,
        mapping_path=args.mapping_snapshot,
        output_dir=args.output_dir,
        trade_date=args.trade_date,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
