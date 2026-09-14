"""Tests for the captured production-chain evidence tool."""

from __future__ import annotations

import json
from pathlib import Path

from examples.run_production_chain_shadow import build_audit_bundle, manifest_summary


def _write_capture(tmp_path: Path, *, include_0924: bool = False) -> Path:
    capture = tmp_path / "capture"
    capture.mkdir(parents=True)
    slots = [
        {
            "name": "auction_0920",
            "status": "ON_TIME",
            "actual_capture_time": "2026-09-14T09:20:05+08:00",
            "artifacts": ["auction_0920.json"],
        },
        {
            "name": "auction_0924",
            "status": "ON_TIME" if include_0924 else "FAILED",
            "actual_capture_time": "2026-09-14T09:24:05+08:00",
            "artifacts": ["auction_0924.json"] if include_0924 else [],
        },
        {
            "name": "auction_0925",
            "status": "ON_TIME",
            "actual_capture_time": "2026-09-14T09:25:10+08:00",
            "artifacts": ["auction_0925.json"],
        },
        {
            "name": "online_q2_093010",
            "status": "ON_TIME",
            "actual_capture_time": "2026-09-14T09:30:10+08:00",
            "artifacts": ["q2_093010.jsonl"],
        },
    ]
    manifest = {
        "trade_date": "2026-09-14",
        "formal_ground_truth": True,
        "sealed": False,
        "slots": slots,
        "runtime_identity_start": {
            "service": "t1",
            "main_pid": 1,
            "binary_sha256": "a",
            "release_git_commit": "commit",
            "build_info_sha256": "build",
            "source_bundle_sha256": "source",
            "exec_main_start_timestamp": "2026-09-14T09:00:00+08:00",
            "restart_count": 0,
        },
        "runtime_identity_end": {
            "service": "t1",
            "main_pid": 1,
            "binary_sha256": "a",
            "release_git_commit": "commit",
            "build_info_sha256": "build",
            "source_bundle_sha256": "source",
            "exec_main_start_timestamp": "2026-09-14T09:00:00+08:00",
            "restart_count": 0,
        },
    }
    (capture / "capture_manifest.partial.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    latest = {
        "meta": json.dumps({"tag": "latest", "ts": 1789348805000, "n": 1}),
        "summary": json.dumps({"total_stocks": 1}),
        "tag": "0920",
        "ts": "1789348805000",
        "top_amount": json.dumps(
            [{"symbol": "600519", "price": 1276, "auction_amount_yuan": 1000}]
        ),
    }
    for slot in ("0920", "0925"):
        (capture / f"auction_{slot}.json").write_text(
            json.dumps({"source_key": f"market:auction:20260914:{slot}", "latest": {**latest, "tag": slot}}),
            encoding="utf-8",
        )
    q2 = {
        "symbol": "600519",
        "px": "1276000",
        "pc": "1275000",
        "amt": "1000",
        "vol": "1",
        "ts": "1789349410000",
        "mk": "sh",
    }
    (capture / "q2_093010.jsonl").write_text(json.dumps(q2) + "\n", encoding="utf-8")
    return capture


def test_manifest_recomputes_partial_truth_instead_of_trusting_formal_flag(tmp_path):
    capture = _write_capture(tmp_path)
    manifest = json.loads((capture / "capture_manifest.partial.json").read_text())
    result = manifest_summary(manifest)
    assert result["declared_formal_ground_truth"] is True
    assert result["recomputed_ground_truth_status"] == "PARTIAL"
    assert result["failed_required_slots"] == ["auction_0924"]
    assert result["runtime_identity_claim"] == "STABLE_OBSERVED"
    assert result["runtime_config_provenance"] == "PARTIAL"


def test_manifest_does_not_call_missing_identity_fields_stable():
    result = manifest_summary(
        {
            "runtime_identity_start": {"main_pid": 1},
            "runtime_identity_end": {"main_pid": 1},
            "slots": [],
        }
    )
    assert result["runtime_identity_observed_equal"] is False
    assert result["runtime_identity_claim"] == "CHANGED_OR_UNPROVEN"


def test_captured_q2_runs_same_core_twice_and_does_not_fabricate_0924(tmp_path):
    capture = _write_capture(tmp_path)
    output = tmp_path / "audit"
    summary = build_audit_bundle(
        capture,
        output,
        trade_date="2026-09-14",
        stale_after_ms=10_000,
    )
    assert summary["q2_engine_shadow"]["symbol_count"] == 1
    assert summary["q2_engine_shadow"]["repeat_hash_equal"] is True
    assert summary["auction"]["auction_0924"]["status"] == "MISSING"
    assert not (output / "auction_0924.json").exists()
    assert (output / "production_chain_matrix.csv").is_file()
    assert (output / "tick_shape_audit.md").is_file()


def test_audit_summary_identity_is_not_machine_directory_name(tmp_path):
    first_capture = _write_capture(tmp_path / "first")
    second_capture = _write_capture(tmp_path / "second")
    first_output = tmp_path / "first-output"
    second_output = tmp_path / "second-output"

    first = build_audit_bundle(
        first_capture,
        first_output,
        trade_date="2026-09-14",
        stale_after_ms=10_000,
    )
    second = build_audit_bundle(
        second_capture,
        second_output,
        trade_date="2026-09-14",
        stale_after_ms=10_000,
    )

    assert first["capture_run_id"] == "capture:2026-09-14"
    assert first == second
    assert (first_output / "audit_summary.json").read_text() == (
        second_output / "audit_summary.json"
    ).read_text()
    for name in (
        "audit_summary.json",
        "tick_shape_samples.jsonl",
        "tick_shape_statistics.csv",
        "tick_shape_audit.md",
    ):
        assert b"\r\n" not in (first_output / name).read_bytes()
