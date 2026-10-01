"""Audit Core opening plate-price facts against pinned legacy replay evidence.

The inputs are an existing real t1-v2 Q2Frame replay, its integrated Core
opening report, the exact date-pinned plate context, and the matching legacy
open-confirmation report. This audit does not connect to Redis, TDengine, or
RabbitMQ and never modifies the input artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core import (  # noqa: E402
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
AUCTION_COMPARE_FIELDS = (
    "auction_positive_ratio",
    "positive_ratio_delta",
    "price_breadth_state",
    "auction_median_change_pct",
    "median_change_pct_delta",
    "median_change_state",
)


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
    auction_stats_by_plate: dict[str, dict[str, Any]] = {}
    legacy_evidence_by_plate: dict[str, dict[str, Any]] = {}
    for observation in observations:
        if not isinstance(observation, Mapping) or not isinstance(
            observation.get("evidence_values"), Mapping
        ):
            raise ValueError("legacy observation is missing evidence_values")
        plate = str(observation.get("plate") or "").strip()
        if not plate or plate in auction_stats_by_plate:
            raise ValueError("legacy observations contain an empty or duplicate plate")
        evidence = dict(observation["evidence_values"])
        if str(evidence.get("plate") or plate) != plate:
            raise ValueError(f"legacy observation plate identity mismatch: {plate}")
        legacy_evidence_by_plate[plate] = evidence
        auction_stats_by_plate[plate] = {
            "positive_ratio": evidence.get("auction_positive_ratio"),
            "median_change_pct": evidence.get("auction_median_change_pct"),
        }

    captured_auction = _load_object(input_dir / "opening_plate_amount_context.json")[
        "source_provenance"
    ]
    if captured_auction.get("auction_rows_sha256") != EXPECTED_INPUT_SHA256[
        "td_auction_snapshot_rows.jsonl"
    ]:
        raise ValueError("plate context does not identify the pinned TD auction rows")
    reference_context = build_opening_plate_price_reference_context(
        trade_date=str(core_report["trade_date"]),
        source_provenance={
            "trade_date": str(core_report["trade_date"]),
            "source": "legacy_open_confirmation.observations.evidence_values",
            "legacy_report_sha256": _sha256(input_dir / "legacy_open_confirmation.json"),
            "legacy_data_origin": legacy_report.get("data_origin"),
            "auction_report_id": legacy_report.get("auction_source", {}).get(
                "report_id"
            ),
            "auction_observation_time": legacy_report.get("auction_source", {}).get(
                "observation_time"
            ),
            "auction_source_table": captured_auction.get("auction_source_table"),
            "auction_rows_sha256": captured_auction.get("auction_rows_sha256"),
            "auction_projection_code_sha256": captured_auction.get(
                "auction_projection_code_sha256"
            ),
            "mapping_snapshot_sha256": captured_auction.get(
                "mapping_snapshot_sha256"
            ),
        },
        auction_price_stats_by_plate=auction_stats_by_plate,
        selected_plates=context["selected_plates"],
    )
    reference_stats_by_plate = reference_context["auction_price_stats_by_plate"]
    if set(reference_stats_by_plate) != set(legacy_evidence_by_plate):
        raise ValueError("legacy auction price references do not match selected plate cohort")

    opening = core_report["ordered"]["opening_evidence"]["OPENING_0932"]
    summary = build_opening_plate_price_summary(
        opening["facts_by_symbol"],
        mapped_symbols_by_plate=context["mapped_symbols_by_plate"],
        auction_symbols_by_plate=context["auction_symbols_by_plate"],
        selected_plates=context["selected_plates"],
        auction_price_reference_by_plate=reference_stats_by_plate,
    )
    legacy_by_plate = {
        str(row["plate"]): row for row in legacy_report.get("plates", [])
    }
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
        for field in OPENING_COMPARE_FIELDS + AUCTION_COMPARE_FIELDS:
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

    audit = {
        "audit": "TASK-008 Core opening plate price facts vs pinned legacy output",
        "status": "PASS_WITH_LIMITS" if not mismatches else "VALUE_MISMATCH",
        "trade_date": core_report["trade_date"],
        "evaluation_time_ms": opening["evaluation_time_ms"],
        "compared_plate_count": len(set(legacy_by_plate) & set(core_by_plate)),
        "compared_fields_per_plate": list(OPENING_COMPARE_FIELDS + AUCTION_COMPARE_FIELDS),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "core_summary_hash": summary["content_hash"],
        "auction_reference_context_hash": reference_context["content_hash"],
        "auction_reference_derivation": {
            "source_fields": [
                "legacy_open_confirmation.observations[].evidence_values.auction_positive_ratio",
                "legacy_open_confirmation.observations[].evidence_values.auction_median_change_pct",
            ],
            "legacy_report_data_origin": legacy_report.get("data_origin"),
            "captured_td_rows_sha256": captured_auction.get("auction_rows_sha256"),
            "independently_recomputed_from_td_rows": False,
        },
        "inputs": {
            "core_report_sha256": _sha256(input_dir / "integrated_core_q2frame_report.json"),
            "legacy_report_sha256": _sha256(input_dir / "legacy_open_confirmation.json"),
            "context_sha256": _sha256(input_dir / "opening_plate_amount_context.json"),
            "prior_parity_sha256": _sha256(input_dir / "opening_plate_amount_parity.json"),
            "q2frame_path": str(q2frame_path),
            "q2frame_sha256": q2frame_sha256,
            "context_source_provenance": context["source_provenance"],
            "auction_reference_source_provenance": reference_context[
                "source_provenance"
            ],
        },
        "limits": [
            "The Q2Frame is real t1-v2 event-time replay, not original Rabbit delivery order.",
            "Historical available_at and Rabbit arrival order remain unknown.",
            "Plate membership uses a frozen mapping and frozen auction cohort; full-market coverage is unproven.",
            "Auction reference values are copied from the hash-pinned legacy report; TD rows are hash-pinned but this small audit does not independently regenerate the auction projection.",
            "The legacy report identifies its data_origin as replay_fixture_only and auction observation_time as unavailable; this comparison is a same-artifact calculation/parity check, not proof of historical availability or live-source timing.",
        ],
        "side_effects": "NONE; pinned local artifacts and pure calculations only",
    }

    for filename, payload in (
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
                "mismatch_count": result["mismatch_count"],
                "core_summary_hash": result["core_summary_hash"],
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result["mismatch_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
