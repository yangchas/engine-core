"""Audit Core opening plate-price facts from hash-pinned replay evidence.

The auction reference is independently derived from captured TD rows and a
frozen stock/plate mapping. A pinned t1-v2 Q2Frame supplies previous-close
values for an arithmetic scale cross-check. The legacy report is only a
comparator. No live Redis, TDengine, or RabbitMQ connection is made.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timezone
from statistics import median
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core import (  # noqa: E402
    classify_delta,
    classify_sign_state,
    build_opening_plate_price_reference_context,
    build_opening_plate_price_summary,
    validate_opening_plate_amount_context,
)

DEFAULT_INPUT_DIR = Path(
    "/home/exedev/validation/task008-opening-plate-amount-integrated-20261001T1142+0800"
)
DEFAULT_Q2FRAME = Path(
    "/home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800/deployed_release_q2frame_to_0932.jsonl"
)
EXPECTED_INPUT_SHA256 = {
    "integrated_core_q2frame_report.json": "fe1b04b0956dff3593ec6b157c60a68bd23004c1bd9a14e853940f8432bb921b",
    "legacy_open_confirmation.json": "9c338e2b2f53675f488c5bac714806692e3b15f0d12533a218e8e57d70474704",
    "opening_plate_amount_context.json": "7c5f01017174f96689ab3d0f3e8cf3cdfbd8da57eb4546eec17fb1b256e3f1c6",
    "opening_plate_amount_parity.json": "b289f95751b29fe6935ffc2ae61ec0573729185e51a217bfab00aa7876f4117a",
    "stock_plate_snapshot.json": "c88eb9339fb1a30dbf6c82da41eebca12f8128a399bd045eaa3da4fbf1a4553b",
    "td_auction_snapshot_rows.jsonl": "b66b78971f6453ad0ab48e5fc818ce7b1e9799afc03bf3e2e9adffae7d44332b",
}
EXPECTED_Q2FRAME_SHA256 = "5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9"
OPENING_COMPARE_FIELDS = (
    "open_valid_count",
    "common_symbol_count",
    "comparison_valid_count",
    "open_up_count",
    "open_down_count",
    "open_flat_count",
    "open_positive_ratio",
    "open_negative_ratio",
    "open_median_change_pct",
    "open_symbols",
)
TD_TAGS = ("0920", "0924", "0925")
SHANGHAI = ZoneInfo("Asia/Shanghai")
SOURCE_CODE_EVIDENCE = {
    "engine_next_td_normalizer": {
        "path": "/home/exedev/services/engine-next/releases/20260903_e272842/engine_next/runtime/production_fact_assembly.py",
        "sha256": "3b145d6225031aacedf3a83f79bff139f51854b45809e469600cf6581180a2c8",
    },
    "engine_next_plate_price_distribution": {
        "path": "/home/exedev/services/engine-next/releases/20260903_e272842/engine_next/runtime/auction_shadow.py",
        "sha256": "a3a99afa64772fc5ab49072318375574007f2f34faae56cac903b45f1d7ebea3",
    },
    "engine_next_open_confirmation_consumer": {
        "path": "/home/exedev/services/engine-next/releases/20260903_e272842/engine_next/runtime/open_confirmation.py",
        "sha256": "73453f5caaf27636c9ad6aa57b2e609da0ad1867ebc1ff06906eca46921405b6",
    },
    "t1_v2_auction_calculator_source_checkout": {
        "path": "/home/exedev/repos/stock-situation-runtime/C/t1_v2/auction_calculator.cpp",
        "sha256": "863907a5270a5f1c2b2b36eeb9cc259b0a7e776e69d47f6554511d3e08722fbc",
        "repository_branch": "codex/task-q2-pure-function",
        "repository_head": "b2aa169c3dddf5d5c7c452f4893dcc01848e358f",
        "producer_build_attestation": "UNVERIFIED",
    },
    "t1_v2_tdengine_auction_writer_source_checkout": {
        "path": "/home/exedev/repos/stock-situation-runtime/C/t1_v2/tdengine_v2_writer.cpp",
        "sha256": "8af65383a778cce9b84f2d2850a677f8592ebd999871e77a1936a2aa0f382931",
        "repository_branch": "codex/task-q2-pure-function",
        "repository_head": "b2aa169c3dddf5d5c7c452f4893dcc01848e358f",
        "producer_build_attestation": "UNVERIFIED",
    },
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"expected JSON object: {path}")
    return dict(value)


def _verify_manifest(input_dir: Path) -> None:
    manifest: dict[str, str] = {}
    for line in (input_dir / "sha256sums.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(maxsplit=1)
        manifest[name.strip()] = digest
    for name, expected in EXPECTED_INPUT_SHA256.items():
        path = input_dir / name
        if manifest.get(name) != expected or _sha256(path) != expected:
            raise ValueError(f"pinned input checksum mismatch: {name}")


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _truncating_integer_ratio(numerator: int, denominator: int) -> int:
    """Match C++ integer division, which truncates toward zero."""

    if denominator <= 0:
        raise ValueError("denominator must be positive")
    quotient = abs(numerator) // denominator
    return quotient if numerator >= 0 else -quotient


def derive_auction_price_stats_from_rows(
    rows: Any,
    *,
    stock_plate: Mapping[str, str],
    selected_plates: tuple[str, ...] | list[str],
    trade_date: str,
    previous_close_by_symbol: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Recompute 0925 plate price facts from captured rows and frozen mapping.

    The TD ``chg_bp`` field is basis points. The deployed Python path converts
    it to percentage points with ``chg_bp / 100`` before distribution. The
    optional Q2 previous-close cross-check independently reconstructs the C++
    basis-point formula for every overlapping symbol.
    """

    if not isinstance(stock_plate, Mapping):
        raise TypeError("stock_plate must be a symbol-to-primary-plate mapping")
    if not isinstance(previous_close_by_symbol, Mapping):
        raise TypeError("previous_close_by_symbol must be a mapping")
    if isinstance(selected_plates, (str, bytes)):
        raise TypeError("selected_plates must be a sequence of plate names")
    selected = sorted(
        {str(plate).strip() for plate in selected_plates if str(plate).strip()}
    )
    mapping: dict[str, str] = {}
    for raw_symbol, raw_plate in stock_plate.items():
        symbol = str(raw_symbol).strip()
        plate = str(raw_plate).strip()
        if not re.fullmatch(r"\d{6}", symbol) or not plate:
            raise ValueError("stock_plate must contain six-digit symbols and non-empty plates")
        mapping[symbol] = plate

    buckets: dict[str, dict[str, Any]] = {
        plate: {"changes_pct": [], "changes_bp": [], "observed_symbols": set(), "unavailable_count": 0}
        for plate in selected
    }
    counts_by_tag = {tag: 0 for tag in TD_TAGS}
    symbols_by_tag: dict[str, set[str]] = {tag: set() for tag in TD_TAGS}
    seen: set[tuple[str, str]] = set()
    duplicate_rows = invalid_symbol_rows = trade_date_mismatch_rows = unknown_tag_rows = malformed_rows = 0
    close_checked = close_mismatches = close_missing = 0
    close_mismatch_samples: list[dict[str, Any]] = []
    expected_trade_date = trade_date.replace("-", "")

    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            malformed_rows += 1
            continue
        tag = str(raw_row.get("auction_tag") or "").strip()
        if tag not in counts_by_tag:
            unknown_tag_rows += 1
            continue
        counts_by_tag[tag] += 1
        if str(raw_row.get("trade_date") or "").replace("-", "") != expected_trade_date:
            trade_date_mismatch_rows += 1
            continue
        symbol = str(raw_row.get("symbol") or "").strip()
        if not re.fullmatch(r"\d{6}", symbol):
            invalid_symbol_rows += 1
            continue
        row_key = (tag, symbol)
        if row_key in seen:
            duplicate_rows += 1
            continue
        seen.add(row_key)
        symbols_by_tag[tag].add(symbol)

        px_milli = _finite_number(raw_row.get("px_milli"))
        chg_bp = _finite_number(raw_row.get("chg_bp"))
        if (
            tag == "0925"
            and px_milli is not None
            and px_milli > 0
            and px_milli.is_integer()
            and chg_bp is not None
            and chg_bp.is_integer()
        ):
            pc_milli = _finite_number(previous_close_by_symbol.get(symbol))
            if pc_milli is None or pc_milli <= 0 or not pc_milli.is_integer():
                close_missing += 1
            else:
                close_checked += 1
                numerator = (int(px_milli) - int(pc_milli)) * 10000
                expected_bp = _truncating_integer_ratio(numerator, int(pc_milli))
                if chg_bp != expected_bp:
                    close_mismatches += 1
                    if len(close_mismatch_samples) < 10:
                        close_mismatch_samples.append(
                            {
                                "symbol": symbol,
                                "td_chg_bp": chg_bp,
                                "reconstructed_chg_bp": expected_bp,
                                "px_milli": px_milli,
                                "pc_milli": pc_milli,
                            }
                        )

        if tag != "0925":
            continue
        plate = mapping.get(symbol)
        if plate not in buckets:
            continue
        bucket = buckets[plate]
        bucket["observed_symbols"].add(symbol)
        if (
            px_milli is None
            or px_milli <= 0
            or not px_milli.is_integer()
            or chg_bp is None
            or not chg_bp.is_integer()
        ):
            bucket["unavailable_count"] += 1
            continue
        bucket["changes_bp"].append(int(chg_bp))
        bucket["changes_pct"].append(int(chg_bp) / 100.0)

    mapped_symbols_by_plate: dict[str, set[str]] = {plate: set() for plate in selected}
    for symbol, plate in mapping.items():
        if plate in mapped_symbols_by_plate:
            mapped_symbols_by_plate[plate].add(symbol)

    stats: dict[str, dict[str, Any]] = {}
    for plate, bucket in buckets.items():
        values_pct = bucket["changes_pct"]
        values_bp = bucket["changes_bp"]
        valid_count = len(values_pct)
        median_bp = median(values_bp) if values_bp else None
        stats[plate] = {
            "mapped_symbol_count": len(mapped_symbols_by_plate[plate]),
            "rows_present_count": len(bucket["observed_symbols"]),
            "missing_row_count": len(mapped_symbols_by_plate[plate]) - len(bucket["observed_symbols"]),
            "valid_count": valid_count,
            "unavailable_count": bucket["unavailable_count"],
            "positive_count": sum(value > 0 for value in values_pct),
            "negative_count": sum(value < 0 for value in values_pct),
            "flat_count": sum(value == 0 for value in values_pct),
            "positive_ratio": (
                sum(value > 0 for value in values_pct) / float(valid_count)
                if valid_count
                else None
            ),
            "median_change_bp": median_bp,
            "median_change_pct": median_bp / 100.0 if median_bp is not None else None,
            "field_unit": "percentage_points; chg_bp / 100",
            "source_completeness": "UNKNOWN",
        }

    diagnostics = {
        "rows_by_tag": counts_by_tag,
        "unique_symbols_by_tag": {tag: len(values) for tag, values in symbols_by_tag.items()},
        "duplicate_tag_symbol_rows": duplicate_rows,
        "invalid_symbol_rows": invalid_symbol_rows,
        "trade_date_mismatch_rows": trade_date_mismatch_rows,
        "unknown_tag_rows": unknown_tag_rows,
        "malformed_rows": malformed_rows,
        "chg_bp_reconstruction": {
            "formula": "trunc_toward_zero((px_milli - pc_milli) * 10000 / pc_milli)",
            "checked_count": close_checked,
            "mismatch_count": close_mismatches,
            "missing_previous_close_count": close_missing,
            "mismatch_samples": close_mismatch_samples,
        },
    }
    return stats, diagnostics


def _close_enough(left: Any, right: Any, *, tolerance: float = 1e-12) -> bool:
    left_number = _finite_number(left)
    right_number = _finite_number(right)
    return (
        left_number is not None
        and right_number is not None
        and math.isclose(left_number, right_number, rel_tol=tolerance, abs_tol=tolerance)
    )


def _load_q2frame_anchor(path: Path, *, expected_trade_date: str) -> tuple[dict[str, int], dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        first_line = handle.readline()
    if not first_line:
        raise ValueError("pinned Q2Frame is empty")
    frame = json.loads(first_line)
    if not isinstance(frame, Mapping) or not isinstance(frame.get("q2_updates"), list):
        raise ValueError("first Q2Frame record is not a Q2FrameV1 object")
    logical_ts_ms = frame.get("logical_ts_ms")
    if isinstance(logical_ts_ms, bool) or not isinstance(logical_ts_ms, int):
        raise ValueError("first Q2Frame logical_ts_ms must be an integer")
    observed_at = datetime.fromtimestamp(logical_ts_ms / 1000, tz=timezone.utc).astimezone(SHANGHAI)
    if observed_at.date().isoformat() != expected_trade_date:
        raise ValueError("first Q2Frame timestamp is outside the expected trade date")
    if observed_at.strftime("%H:%M:%S") != "09:15:00":
        raise ValueError("first Q2Frame is not the expected 09:15 anchor")

    previous_close_by_symbol: dict[str, int] = {}
    duplicate_symbols: list[str] = []
    invalid_symbols = invalid_previous_close = 0
    for update in frame["q2_updates"]:
        if not isinstance(update, Mapping):
            invalid_symbols += 1
            continue
        symbol = str(update.get("symbol") or "").strip()
        if not re.fullmatch(r"\d{6}", symbol):
            invalid_symbols += 1
            continue
        if symbol in previous_close_by_symbol:
            duplicate_symbols.append(symbol)
            continue
        pc = _finite_number(update.get("pc"))
        if pc is None or pc <= 0 or not pc.is_integer():
            invalid_previous_close += 1
            continue
        previous_close_by_symbol[symbol] = int(pc)
    diagnostics = {
        "contract": frame.get("version"),
        "logical_ts_ms": logical_ts_ms,
        "observed_at": observed_at.isoformat(),
        "phase": frame.get("phase"),
        "seq_no": frame.get("seq_no"),
        "update_count": len(frame["q2_updates"]),
        "valid_previous_close_count": len(previous_close_by_symbol),
        "invalid_symbol_count": invalid_symbols,
        "invalid_previous_close_count": invalid_previous_close,
        "duplicate_symbols": sorted(set(duplicate_symbols)),
    }
    return previous_close_by_symbol, diagnostics


def run_audit(*, input_dir: Path, q2frame_path: Path, output_dir: Path) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError("output directory must be new or empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    _verify_manifest(input_dir)
    q2frame_sha256 = _sha256(q2frame_path)
    if q2frame_sha256 != EXPECTED_Q2FRAME_SHA256:
        raise ValueError("pinned Q2Frame checksum mismatch")

    core_report = _load_object(input_dir / "integrated_core_q2frame_report.json")
    legacy_report = _load_object(input_dir / "legacy_open_confirmation.json")
    prior_parity = _load_object(input_dir / "opening_plate_amount_parity.json")
    context = validate_opening_plate_amount_context(
        _load_object(input_dir / "opening_plate_amount_context.json"),
        trade_date=str(core_report.get("trade_date") or ""),
    )
    if core_report.get("q2frame", {}).get("sha256") != q2frame_sha256:
        raise ValueError("Core report does not identify the pinned Q2Frame")
    if prior_parity.get("inputs", {}).get("q2frame_sha256") != q2frame_sha256:
        raise ValueError("prior same-date audit does not identify the pinned Q2Frame")
    if legacy_report.get("trade_date") != core_report.get("trade_date"):
        raise ValueError("legacy report and Core report trade dates differ")

    observations = legacy_report.get("observations")
    if not isinstance(observations, list):
        raise ValueError("legacy report must contain per-plate observations")
    legacy_evidence_by_plate: dict[str, dict[str, Any]] = {}
    for observation in observations:
        if not isinstance(observation, Mapping) or not isinstance(
            observation.get("evidence_values"), Mapping
        ):
            raise ValueError("legacy observation is missing evidence_values")
        plate = str(observation.get("plate") or "").strip()
        if not plate or plate in legacy_evidence_by_plate:
            raise ValueError("legacy observations contain an empty or duplicate plate")
        evidence = dict(observation["evidence_values"])
        if str(evidence.get("plate") or plate) != plate:
            raise ValueError(f"legacy observation plate identity mismatch: {plate}")
        legacy_evidence_by_plate[plate] = evidence
    raw_context = _load_object(input_dir / "opening_plate_amount_context.json")
    captured_auction = raw_context["source_provenance"]
    if captured_auction.get("auction_rows_sha256") != EXPECTED_INPUT_SHA256[
        "td_auction_snapshot_rows.jsonl"
    ]:
        raise ValueError("plate context does not identify the pinned TD auction rows")
    mapping_snapshot = _load_object(input_dir / "stock_plate_snapshot.json")
    if mapping_snapshot.get("trade_date") != core_report.get("trade_date"):
        raise ValueError("stock/plate mapping snapshot trade date differs from replay")
    if mapping_snapshot.get("sha256") != captured_auction.get("mapping_data_sha256"):
        raise ValueError("stock/plate mapping content hash differs from captured provenance")
    if mapping_snapshot.get("record_count") != len(mapping_snapshot.get("mapping", {})):
        raise ValueError("stock/plate mapping record_count does not match mapping contents")
    if _sha256(input_dir / "stock_plate_snapshot.json") != captured_auction.get(
        "mapping_snapshot_sha256"
    ):
        raise ValueError("stock/plate mapping snapshot file hash mismatch")
    selected_plates = tuple(context["selected_plates"])
    stock_plate = mapping_snapshot.get("mapping")
    if not isinstance(stock_plate, Mapping):
        raise ValueError("stock/plate snapshot does not contain a mapping object")
    mapped_symbols_by_plate = {
        plate: sorted(symbol for symbol, mapped_plate in stock_plate.items() if mapped_plate == plate)
        for plate in selected_plates
    }
    context_symbols_by_plate = {
        plate: set(context["mapped_symbols_by_plate"][plate])
        for plate in selected_plates
    }
    mapping_conflicts = {
        plate: sorted(
            symbol
            for symbol in context_symbols_by_plate[plate]
            if stock_plate.get(symbol) != plate
        )
        for plate in selected_plates
    }
    if any(mapping_conflicts.values()):
        raise ValueError(f"frozen context contains members that conflict with the mapping snapshot: {mapping_conflicts}")
    # Keep the exact frozen context cohort used by the prior Core/legacy
    # comparison. The mapping snapshot contains a broader source universe;
    # symbols outside this selected cohort are not silently added here.
    analysis_stock_plate = {
        symbol: plate
        for plate, symbols in context_symbols_by_plate.items()
        for symbol in symbols
    }
    mapping_scope = {
        "snapshot_symbol_count": len(stock_plate),
        "selected_context_symbol_count": len(analysis_stock_plate),
        "snapshot_selected_plate_counts": {
            plate: len(mapped_symbols_by_plate[plate]) for plate in selected_plates
        },
        "context_selected_plate_counts": {
            plate: len(context_symbols_by_plate[plate]) for plate in selected_plates
        },
        "snapshot_extra_selected_plate_symbols": {
            plate: sorted(set(mapped_symbols_by_plate[plate]) - context_symbols_by_plate[plate])
            for plate in selected_plates
        },
        "context_membership_conflicts": mapping_conflicts,
        "scope_rule": "use exact hash-pinned opening_plate_amount_context membership; mapping snapshot is a broader universe",
    }

    previous_close_by_symbol, q2frame_anchor = _load_q2frame_anchor(
        q2frame_path, expected_trade_date=str(core_report["trade_date"])
    )
    rows_path = input_dir / "td_auction_snapshot_rows.jsonl"

    def iter_rows():
        with rows_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid TD JSONL row at line {line_number}") from exc

    auction_stats_by_plate, td_diagnostics = derive_auction_price_stats_from_rows(
        iter_rows(),
        stock_plate=analysis_stock_plate,
        selected_plates=selected_plates,
        trade_date=str(core_report["trade_date"]),
        previous_close_by_symbol=previous_close_by_symbol,
    )
    source_code_checks = {
        name: Path(evidence["path"]).is_file()
        and _sha256(Path(evidence["path"])) == evidence["sha256"]
        for name, evidence in SOURCE_CODE_EVIDENCE.items()
    }
    expected_rows_by_tag = captured_auction.get("auction_rows_by_tag")
    evidence_checks = {
        "rows_by_tag_match_manifest": td_diagnostics["rows_by_tag"] == expected_rows_by_tag,
        "no_duplicate_tag_symbol_rows": td_diagnostics["duplicate_tag_symbol_rows"] == 0,
        "no_invalid_symbols": td_diagnostics["invalid_symbol_rows"] == 0,
        "no_trade_date_mismatch": td_diagnostics["trade_date_mismatch_rows"] == 0,
        "no_unknown_tags": td_diagnostics["unknown_tag_rows"] == 0,
        "no_malformed_rows": td_diagnostics["malformed_rows"] == 0,
        "q2_anchor_has_no_duplicate_symbols": not q2frame_anchor["duplicate_symbols"],
        "positive_close_reconstruction_sample": td_diagnostics["chg_bp_reconstruction"]["checked_count"] > 0,
        "no_chg_bp_reconstruction_mismatch": td_diagnostics["chg_bp_reconstruction"]["mismatch_count"] == 0,
    }
    if not all(evidence_checks.values()):
        raise ValueError(f"pinned input evidence validation failed: {evidence_checks}")

    legacy_by_plate = {
        str(row["plate"]): row for row in legacy_report.get("plates", [])
    }
    if set(auction_stats_by_plate) != set(legacy_evidence_by_plate):
        raise ValueError("raw TD selected plates do not match legacy observation cohort")
    legacy_comparisons: dict[str, dict[str, Any]] = {}
    count_ratio_mismatches: list[dict[str, Any]] = []
    legacy_unit_mismatches: list[dict[str, Any]] = []
    for plate in sorted(auction_stats_by_plate):
        raw_stats = auction_stats_by_plate[plate]
        legacy = legacy_evidence_by_plate[plate]
        positive_ratio_matches = _close_enough(
            raw_stats["positive_ratio"], legacy.get("auction_positive_ratio")
        )
        valid_count_matches = raw_stats["valid_count"] == legacy.get("auction_valid_count")
        raw_median_bp = raw_stats["median_change_bp"]
        legacy_median = _finite_number(legacy.get("auction_median_change_pct"))
        raw_median_pct = raw_stats["median_change_pct"]
        median_is_bp_scaled = (
            legacy_median is not None
            and raw_median_bp is not None
            and _close_enough(legacy_median, raw_median_bp)
        )
        median_matches_normalized_pct = _close_enough(legacy_median, raw_median_pct)
        legacy_comparisons[plate] = {
            "valid_count": raw_stats["valid_count"],
            "legacy_valid_count": legacy.get("auction_valid_count"),
            "valid_count_matches": valid_count_matches,
            "positive_ratio": raw_stats["positive_ratio"],
            "legacy_positive_ratio": legacy.get("auction_positive_ratio"),
            "positive_ratio_matches": positive_ratio_matches,
            "raw_median_change_bp": raw_median_bp,
            "normalized_median_change_pct": raw_median_pct,
            "legacy_reported_auction_median_change_pct": legacy_median,
            "legacy_median_equals_raw_bp": median_is_bp_scaled,
            "legacy_median_matches_normalized_pct": median_matches_normalized_pct,
            "legacy_unit_relation": (
                "LEGACY_VALUE_IS_RAW_BASIS_POINTS" if median_is_bp_scaled and not median_matches_normalized_pct
                else "MATCHES_NORMALIZED_PERCENTAGE_POINTS" if median_matches_normalized_pct
                else "UNEXPLAINED_MISMATCH"
            ),
        }
        if not valid_count_matches or not positive_ratio_matches:
            count_ratio_mismatches.append({"plate": plate, **legacy_comparisons[plate]})
        if median_is_bp_scaled and not median_matches_normalized_pct:
            legacy_unit_mismatches.append({"plate": plate, **legacy_comparisons[plate]})

    reference_context = build_opening_plate_price_reference_context(
        trade_date=str(core_report["trade_date"]),
        source_provenance={
            "trade_date": str(core_report["trade_date"]),
            "source": "market_data1.auction_snapshot_v2.0925.td_rows",
            "auction_source_table": captured_auction.get("auction_source_table"),
            "auction_rows_sha256": captured_auction.get("auction_rows_sha256"),
            "auction_rows_file_sha256": _sha256(rows_path),
            "auction_metric_formula": "median(chg_bp) / 100; positive_count / valid_count",
            "chg_bp_unit": "basis_points",
            "percentage_unit": "percentage_points",
            "mapping_snapshot_sha256": captured_auction.get("mapping_snapshot_sha256"),
            "q2frame_path": str(q2frame_path),
            "q2frame_sha256": q2frame_sha256,
            "q2frame_first_anchor": q2frame_anchor,
            "previous_close_source": "first pinned t1-v2 Q2Frame q2_updates[].pc",
            "chg_bp_reconstruction": td_diagnostics["chg_bp_reconstruction"],
            "source_code_evidence": SOURCE_CODE_EVIDENCE,
            "producer_build_attestation": "UNVERIFIED",
        },
        auction_price_stats_by_plate=auction_stats_by_plate,
        selected_plates=context["selected_plates"],
    )
    reference_stats_by_plate = reference_context["auction_price_stats_by_plate"]
    if set(reference_stats_by_plate) != set(legacy_evidence_by_plate):
        raise ValueError("TD auction price references do not match selected plate cohort")

    opening = core_report["ordered"]["opening_evidence"]["OPENING_0932"]
    summary = build_opening_plate_price_summary(
        opening["facts_by_symbol"],
        mapped_symbols_by_plate=context["mapped_symbols_by_plate"],
        auction_symbols_by_plate=context["auction_symbols_by_plate"],
        selected_plates=context["selected_plates"],
        auction_price_reference_by_plate=reference_stats_by_plate,
    )
    core_by_plate = {str(row["plate"]): row for row in summary["plates"]}
    mismatches: list[dict[str, Any]] = []
    for plate in sorted(set(legacy_by_plate) | set(core_by_plate)):
        if plate not in legacy_by_plate or plate not in core_by_plate:
            mismatches.append(
                {
                    "plate": plate,
                    "field": "plate_membership",
                    "legacy_present": plate in legacy_by_plate,
                    "core_present": plate in core_by_plate,
                }
            )
            continue
        for field in OPENING_COMPARE_FIELDS:
            legacy_value = legacy_evidence_by_plate[plate].get(
                field, legacy_by_plate[plate].get(field)
            )
            core_value = core_by_plate[plate].get(field)
            if legacy_value != core_value:
                mismatches.append(
                    {
                        "plate": plate,
                        "field": field,
                        "legacy": legacy_value,
                        "core": core_value,
                    }
                )

    core_formula_mismatches: list[dict[str, Any]] = []
    for plate, raw_stats in auction_stats_by_plate.items():
        core_row = core_by_plate.get(plate)
        if core_row is None:
            core_formula_mismatches.append({"plate": plate, "field": "plate_membership"})
            continue
        expected_delta = classify_delta(
            (core_row.get("open_positive_ratio") - raw_stats["positive_ratio"])
            if core_row.get("open_positive_ratio") is not None and raw_stats["positive_ratio"] is not None
            else None
        )
        expected_median_delta = (
            core_row.get("open_median_change_pct") - raw_stats["median_change_pct"]
            if core_row.get("open_median_change_pct") is not None and raw_stats["median_change_pct"] is not None
            else None
        )
        expected_median_state = classify_sign_state(
            raw_stats["median_change_pct"], core_row.get("open_median_change_pct")
        )
        expected_values = {
            "auction_positive_ratio": raw_stats["positive_ratio"],
            "auction_median_change_pct": raw_stats["median_change_pct"],
            "positive_ratio_delta": (
                core_row.get("open_positive_ratio") - raw_stats["positive_ratio"]
                if core_row.get("open_positive_ratio") is not None
                else None
            ),
            "price_breadth_state": expected_delta,
            "median_change_pct_delta": expected_median_delta,
            "median_change_state": expected_median_state,
        }
        checks = {
            "auction_positive_ratio": _close_enough(core_row.get("auction_positive_ratio"), raw_stats["positive_ratio"]),
            "auction_median_change_pct": _close_enough(core_row.get("auction_median_change_pct"), raw_stats["median_change_pct"]),
            "positive_ratio_delta": _close_enough(core_row.get("positive_ratio_delta"), expected_values["positive_ratio_delta"]),
            "price_breadth_state": core_row.get("price_breadth_state") == expected_delta,
            "median_change_pct_delta": _close_enough(core_row.get("median_change_pct_delta"), expected_median_delta),
            "median_change_state": core_row.get("median_change_state") == expected_median_state,
        }
        for field, passed in checks.items():
            if not passed:
                core_formula_mismatches.append({
                    "plate": plate,
                    "field": field,
                    "core": core_row.get(field),
                    "raw_derived_expected": expected_values[field],
                })

    legacy_unit_relation_proven = (
        len(legacy_unit_mismatches) == len(selected_plates)
        and all(
            legacy_comparisons[plate]["legacy_unit_relation"]
            == "LEGACY_VALUE_IS_RAW_BASIS_POINTS"
            for plate in selected_plates
        )
    )
    core_raw_mismatch_count = len(mismatches) + len(core_formula_mismatches)
    audit_pass = (
        core_raw_mismatch_count == 0
        and not count_ratio_mismatches
        and legacy_unit_relation_proven
        and all(source_code_checks.values())
    )
    audit = {
        "audit": "TASK-008 Core opening plate price facts recomputed from pinned TD rows",
        "status": "CORE_TD_RECOMPUTE_PASS_WITH_LEGACY_UNIT_DIVERGENCE" if audit_pass else "REVIEW_REQUIRED",
        "trade_date": core_report["trade_date"],
        "evaluation_time_ms": opening["evaluation_time_ms"],
        "compared_plate_count": len(set(legacy_by_plate) & set(core_by_plate)),
        "compared_fields_per_plate": list(OPENING_COMPARE_FIELDS),
        "core_opening_mismatch_count": len(mismatches),
        "core_opening_mismatches": mismatches,
        "core_raw_formula_mismatch_count": len(core_formula_mismatches),
        "core_raw_formula_mismatches": core_formula_mismatches,
        "core_raw_mismatch_count": core_raw_mismatch_count,
        "legacy_valid_count_or_ratio_mismatch_count": len(count_ratio_mismatches),
        "legacy_valid_count_or_ratio_mismatches": count_ratio_mismatches,
        "legacy_unit_mismatch_count": len(legacy_unit_mismatches),
        "legacy_unit_relation": "legacy_reported_median equals raw median chg_bp, while Core uses chg_bp / 100 percentage points",
        "legacy_comparisons_by_plate": legacy_comparisons,
        "input_evidence_checks": evidence_checks,
        "source_code_hash_checks": source_code_checks,
        "td_row_diagnostics": td_diagnostics,
        "q2frame_first_anchor": q2frame_anchor,
        "mapping_scope": mapping_scope,
        "q2_previous_close_symbol_count": len(previous_close_by_symbol),
        "q2_invalid_previous_close_count": q2frame_anchor["invalid_previous_close_count"],
        "legacy_report_data_origin": legacy_report.get("data_origin"),
        "legacy_auction_observation_time": legacy_report.get("auction_source", {}).get("observation_time"),
        "core_summary_hash": summary["content_hash"],
        "auction_reference_context_hash": reference_context["content_hash"],
        "auction_reference_derivation": {
            "source_fields": ["td_auction_snapshot_rows.jsonl rows tagged 0925: chg_bp"],
            "mapping_source": "hash-pinned stock_plate_snapshot.json .mapping",
            "independently_recomputed_from_td_rows": True,
            "q2_previous_close_formula_cross_check": td_diagnostics["chg_bp_reconstruction"],
        },
        "inputs": {
            "core_report_sha256": _sha256(input_dir / "integrated_core_q2frame_report.json"),
            "legacy_report_sha256": _sha256(input_dir / "legacy_open_confirmation.json"),
            "context_sha256": _sha256(input_dir / "opening_plate_amount_context.json"),
            "prior_parity_sha256": _sha256(input_dir / "opening_plate_amount_parity.json"),
            "mapping_snapshot_sha256": _sha256(input_dir / "stock_plate_snapshot.json"),
            "td_rows_sha256": _sha256(rows_path),
            "q2frame_path": str(q2frame_path),
            "q2frame_sha256": q2frame_sha256,
            "context_source_provenance": context["source_provenance"],
            "auction_reference_source_provenance": reference_context["source_provenance"],
        },
        "limits": [
            "The Q2Frame is real t1-v2 event-time replay, not original Rabbit delivery order.",
            "Historical available_at and Rabbit arrival order remain unknown.",
            "Plate membership uses a frozen mapping and frozen auction cohort; full-market coverage is unproven.",
            "TD row completeness remains UNKNOWN; captured row counts and exact hashes are verified, but this does not prove no upstream omissions.",
            "The legacy report identifies data_origin=replay_fixture_only and auction observation_time=unavailable; its median field is numerically equal to raw chg_bp rather than percentage points in all selected plates, so legacy median deltas are not treated as a Core parity oracle.",
            "Q2Frame previous-close arithmetic cross-check validates the chg_bp scale for overlapping symbols; the exact producer binary/build is not attested.",
            "The legacy report's auction observation time is unavailable; this cannot establish when those facts were available to a live consumer.",
        ],
        "side_effects": "NONE; pinned local artifacts and pure calculations only",
    }

    for filename, payload in (
        ("auction_price_stats_from_td.json", {"trade_date": core_report["trade_date"], "source": captured_auction["auction_source_table"], "source_rows_sha256": _sha256(rows_path), "diagnostics": td_diagnostics, "stats_by_plate": auction_stats_by_plate}),
        ("opening_plate_price_reference_context.json", reference_context),
        ("opening_plate_price_summary.json", summary),
        ("audit_summary.json", audit),
    ):
        (output_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    sums = []
    for path in sorted(output_dir.iterdir()):
        if path.is_file() and path.name != "sha256sums.txt":
            sums.append(f"{_sha256(path)}  {path.name}")
    (output_dir / "sha256sums.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--q2frame", type=Path, default=DEFAULT_Q2FRAME)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = run_audit(input_dir=args.input_dir, q2frame_path=args.q2frame, output_dir=args.output_dir)
    print(
        json.dumps(
            {
                "status": result["status"],
                "compared_plate_count": result["compared_plate_count"],
                "core_raw_mismatch_count": result["core_raw_mismatch_count"],
                "core_opening_mismatch_count": result["core_opening_mismatch_count"],
                "core_raw_formula_mismatch_count": result["core_raw_formula_mismatch_count"],
                "legacy_unit_mismatch_count": result["legacy_unit_mismatch_count"],
                "q2_chg_bp_reconstruction_checked": result["td_row_diagnostics"]["chg_bp_reconstruction"]["checked_count"],
                "q2_chg_bp_reconstruction_mismatch": result["td_row_diagnostics"]["chg_bp_reconstruction"]["mismatch_count"],
                "core_summary_hash": result["core_summary_hash"],
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result["status"] == "CORE_TD_RECOMPUTE_PASS_WITH_LEGACY_UNIT_DIVERGENCE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
