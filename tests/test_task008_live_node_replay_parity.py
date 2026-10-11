"""Synthetic harness controls; these are not real-market replay evidence."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from engine_core import FreshnessPolicy, Q2FrameReplaySource, VirtualClock
from engine_core.windows import local_time_ms
import examples.run_task008_live_node_replay_parity as parity_runner
from examples.run_task008_live_node_replay_parity import (
    BatchCheckpoint,
    _aggregate_row_hash,
    _implementation_fingerprints,
    _node_timer_firing,
    _publish_final_evidence,
    _row_hash,
    _run_live_node_rows_in_batches,
    _select_repeat_symbols,
    compare_opening_fact_maps,
)


def test_synthetic_fact_comparison_counts_equal_different_and_missing_fields():
    core = {
        "000001": {
            "symbol": "000001",
            "status": "available",
            "change_pct": 1.25,
            "amount_2m_yuan": None,
            "core_only_field": 4,
        },
        "000002": {"symbol": "000002", "status": "available", "change_pct": 0.0},
    }
    live = {
        "000001": {
            "symbol": "000001",
            "status": "available",
            "change_pct": 1.25,
            "live_only_field": 5,
        },
        "000003": {"symbol": "000003", "status": "partial", "change_pct": 0.5},
    }

    result = compare_opening_fact_maps(core, live, detail_limit=10)

    assert result["comparison_status"] == "DIFFERENCES"
    assert result["symbol_counts"] == {
        "core_symbol_count": 2,
        "live_symbol_count": 2,
        "shared_symbol_count": 1,
        "core_only_symbol_count": 1,
        "live_only_symbol_count": 1,
    }
    assert result["field_counts"]["change_pct"] == {
        "equal": 1,
        "different": 0,
        "missing_core_field": 0,
        "missing_live_field": 0,
    }
    assert result["field_counts"]["amount_2m_yuan"] == {
        "equal": 0,
        "different": 0,
        "missing_core_field": 0,
        "missing_live_field": 1,
    }
    assert result["field_counts"]["core_only_field"]["missing_live_field"] == 1
    assert result["field_counts"]["live_only_field"]["missing_core_field"] == 1
    assert result["difference_details"]
    assert result["difference_details_truncated"] is False


def test_synthetic_fact_comparison_keeps_explicit_null_distinct_from_absence():
    result = compare_opening_fact_maps(
        {"000001": {"amount_2m_yuan": None}},
        {"000001": {}},
    )

    assert result["comparison_status"] == "DIFFERENCES"
    assert result["field_counts"]["amount_2m_yuan"] == {
        "equal": 0,
        "different": 0,
        "missing_core_field": 0,
        "missing_live_field": 1,
    }


def _synthetic_projection():
    """Build a tiny synthetic Q2Frame projection for harness parity controls."""
    trade_date = "2026-09-30"
    symbols = ("000001", "000002", "600519")
    evaluation_ms = local_time_ms(trade_date, "09:32:10")
    observed = datetime.fromtimestamp(evaluation_ms / 1000, tz=timezone.utc)
    source = Q2FrameReplaySource(
        trade_date,
        symbols,
        VirtualClock(observed),
        freshness_policy=FreshnessPolicy(stale_after_ms=60_000, max_future_skew_ms=999),
        source_id="synthetic_control_only",
    )
    projection = source.apply(
        {
            "version": "Q2FrameV1",
            "seq_no": 1,
            "logical_ts_ms": evaluation_ms,
            "q2_updates": [
                {
                    "symbol": symbol,
                    "mk": "SH" if symbol.startswith(("6", "9")) else "SZ",
                    "px": str(10000 + index * 100),
                    "pc": "10000",
                    "amt": str(1_000_000 + index),
                    "vol": str(100 + index),
                    "amt2m": str(50_000 + index),
                    "ts": str(evaluation_ms),
                }
                for index, symbol in enumerate(symbols)
            ],
        }
    )
    firing = _node_timer_firing(trade_date, evaluation_ms)[1]
    return trade_date, symbols, evaluation_ms, observed, projection, firing


def test_synthetic_live_node_rows_are_batch_size_independent():
    trade_date, symbols, evaluation_ms, observed, projection, firing = _synthetic_projection()

    by_batch_size = {
        batch_size: _run_live_node_rows_in_batches(
            firing,
            observed_at=observed,
            trade_date=trade_date,
            symbols=symbols,
            projection=projection,
            batch_size=batch_size,
        )
        for batch_size in (1, 2, 3, 100)
    }

    expected_hashes = {
        symbol: by_batch_size[3][symbol]["row_hash"] for symbol in symbols
    }
    for rows in by_batch_size.values():
        assert {symbol: rows[symbol]["row_hash"] for symbol in symbols} == expected_hashes
        assert _aggregate_row_hash(rows) == _aggregate_row_hash(by_batch_size[3])
    assert projection.expected_symbols == symbols
    assert projection.content_hash


def test_live_builder_rows_survive_checkpoint_json_round_trip(tmp_path):
    trade_date, symbols, _, observed, projection, firing = _synthetic_projection()
    rows = _run_live_node_rows_in_batches(
        firing,
        observed_at=observed,
        trade_date=trade_date,
        symbols=symbols,
        projection=projection,
        batch_size=len(symbols),
    )
    checkpoint_dir = tmp_path / "checkpoint" / "first_pass"
    checkpoint = BatchCheckpoint(
        checkpoint_dir,
        phase="first_pass",
        manifest={"projection_hash": projection.content_hash},
        expected_symbols=symbols,
        batch_size=2,
        python_hash_seed="1",
        resume=False,
    )
    for batch_index, start in enumerate(range(0, len(symbols), 2)):
        batch = symbols[start : start + 2]
        checkpoint.commit_batch(batch_index, batch, {symbol: rows[symbol] for symbol in batch})

    restored = BatchCheckpoint(
        checkpoint_dir,
        phase="first_pass",
        manifest={"projection_hash": projection.content_hash},
        expected_symbols=symbols,
        batch_size=2,
        python_hash_seed="1",
        resume=True,
    )
    assert {symbol: row["row_hash"] for symbol, row in restored.completed_rows.items()} == {
        symbol: row["row_hash"] for symbol, row in rows.items()
    }
    assert _aggregate_row_hash(restored.completed_rows) == _aggregate_row_hash(rows)


def test_resource_errors_abort_without_becoming_checkpoint_rows(monkeypatch):
    trade_date, symbols, _, observed, projection, firing = _synthetic_projection()

    for error in (MemoryError("synthetic memory pressure"), OSError("synthetic local IO failure")):
        def fail_builder(*args, **kwargs):
            raise error

        monkeypatch.setattr(parity_runner, "_run_live_node", fail_builder)
        with pytest.raises(type(error)):
            parity_runner._run_one_node_batch(
                firing,
                observed_at=observed,
                trade_date=trade_date,
                symbols=symbols[:2],
                projection=projection,
            )


def test_batch_fallback_error_types_are_available_for_durable_metrics(monkeypatch):
    trade_date, symbols, _, observed, projection, firing = _synthetic_projection()

    def fail_multi_symbol(*args, **kwargs):
        if len(kwargs["symbols"]) > 1:
            raise ValueError("synthetic batch-only failure")
        return {"fact_dispatch": []}

    def make_rows(_node, batch_symbols):
        return {
            symbol: {
                "symbol": symbol,
                "row_outcome": "NO_OPENING_FACT",
                "fact_status": "UNKNOWN",
                "opening_fact": None,
                "field_status": None,
                "engine_status": "UNKNOWN",
                "engine_error_type": None,
                "row_hash": "stable-placeholder",
            }
            for symbol in batch_symbols
        }

    monkeypatch.setattr(parity_runner, "_run_live_node", fail_multi_symbol)
    monkeypatch.setattr(parity_runner, "_rows_from_live_node", make_rows)
    rows, fallback_count, error_types = parity_runner._run_one_node_batch(
        firing,
        observed_at=observed,
        trade_date=trade_date,
        symbols=symbols[:2],
        projection=projection,
    )

    assert set(rows) == set(symbols[:2])
    assert fallback_count == 2
    assert error_types == ("builtins.ValueError",)


def test_run_fingerprint_covers_core_examples_and_dirty_tree_identity():
    fingerprint = _implementation_fingerprints()

    assert fingerprint["contract"] == "Task008ImplementationFingerprintV2"
    assert "src/engine_core/opening.py" in fingerprint["files"]
    assert "examples/run_morning_vertical_slice_shadow.py" in fingerprint["files"]
    assert "workspace_diff_head_sha256" in fingerprint
    assert "workspace_status_sha256" in fingerprint


def test_cli_resume_then_independent_repeat_completes_synthetic_smoke(tmp_path):
    trade_date, symbols, evaluation_ms, observed, projection, firing = _synthetic_projection()
    q2frame_path = tmp_path / "q2frame.jsonl"
    q2frame_path.write_text(
        json.dumps(
            {
                "version": "Q2FrameV1",
                "seq_no": 1,
                "logical_ts_ms": evaluation_ms,
                "q2_updates": [
                    {
                        "symbol": symbol,
                        "mk": "SH" if symbol.startswith(("6", "9")) else "SZ",
                        "px": str(10000 + index * 100),
                        "pc": "10000",
                        "amt": str(1_000_000 + index),
                        "vol": str(100 + index),
                        "amt2m": str(50_000 + index),
                        "ts": str(evaluation_ms),
                    }
                    for index, symbol in enumerate(symbols)
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    q2frame_sha = hashlib.sha256(q2frame_path.read_bytes()).hexdigest()
    core_report = {
        "trade_date": trade_date,
        "q2frame": {"sha256": q2frame_sha},
        "ordered": {
            "opening_evidence": {
                "OPENING_0932": {
                    "evaluation_time_ms": evaluation_ms,
                    "expected_symbol_count": len(symbols),
                    "facts_by_symbol": {symbol: {"symbol": symbol} for symbol in symbols},
                    "opening_fact_field_status_by_symbol": {
                        symbol: {} for symbol in symbols
                    },
                    "freshness_policy": {"stale_after_ms": 60_000},
                }
            }
        },
    }
    core_report_path = tmp_path / "core_report.json"
    core_report_path.write_text(json.dumps(core_report), encoding="utf-8")
    core_report_sha = hashlib.sha256(core_report_path.read_bytes()).hexdigest()
    run_dir = tmp_path / "cli-smoke"
    final_path = run_dir / "final.json"
    runner_path = Path(parity_runner.__file__).resolve()
    project_root = runner_path.parents[1]

    context = parity_runner._load_run_context(
        q2frame_path=q2frame_path,
        core_report_path=core_report_path,
        expected_q2frame_sha256=q2frame_sha,
        expected_core_report_sha256=core_report_sha,
    )
    fingerprint = parity_runner._run_fingerprint(
        context,
        batch_size=1,
        repeat_sample_size=1,
        detail_limit=1,
        primary_hash_seed="101",
    )
    parity_runner._ensure_run_manifest(run_dir, fingerprint, resume=False)
    checkpoint = BatchCheckpoint(
        run_dir / "checkpoint" / "first_pass",
        phase="first_pass",
        manifest={"fingerprint": fingerprint},
        expected_symbols=symbols,
        batch_size=1,
        python_hash_seed="101",
        resume=False,
    )
    first_batch, fallback_count, error_types = parity_runner._run_one_node_batch(
        firing,
        observed_at=observed,
        trade_date=trade_date,
        symbols=symbols[:1],
        projection=projection,
    )
    checkpoint.commit_batch(
        0,
        symbols[:1],
        first_batch,
        metrics={
            "batch_elapsed_seconds": 0.0,
            "fallback_symbol_count": fallback_count,
            "batch_error_types": list(error_types),
        },
    )

    common_args = [
        str(runner_path),
        "--q2frame",
        str(q2frame_path),
        "--expected-q2frame-sha256",
        q2frame_sha,
        "--core-report",
        str(core_report_path),
        "--expected-core-report-sha256",
        core_report_sha,
        "--output",
        str(final_path),
        "--batch-size",
        "1",
        "--repeat-sample-size",
        "1",
        "--detail-limit",
        "1",
    ]
    first_env = {**os.environ, "PYTHONHASHSEED": "101"}
    resumed = subprocess.run(
        [sys.executable, *common_args, "--resume"],
        cwd=project_root,
        env=first_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert resumed.returncode == 0, resumed.stderr

    repeat_env = {**os.environ, "PYTHONHASHSEED": "202"}
    repeated = subprocess.run(
        [sys.executable, *common_args, "--repeat-only"],
        cwd=project_root,
        env=repeat_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert repeated.returncode == 0, repeated.stderr

    report = json.loads(final_path.read_text(encoding="utf-8"))
    assert report["phase_status"] == "COMPLETE"
    assert report["completion"]["all_expected_symbols_accounted_for"] is True
    assert report["live_node_first_pass"]["completed_symbol_count"] == len(symbols)
    assert report["determinism_repeat"]["repeat_python_hash_seed"] == "202"
    assert report["determinism_repeat"]["repeat_process_ids"] != report[
        "determinism_repeat"
    ]["primary_process_ids"]
    assert set(report["determinism_repeat"]["primary_process_instance_ids"]).isdisjoint(
        report["determinism_repeat"]["repeat_process_instance_ids"]
    )
    assert report["run_cost_observation"][
        "first_pass_active_batch_elapsed_seconds"
    ] is not None
    for line in (run_dir / "sha256sums.txt").read_text(encoding="utf-8").splitlines():
        expected_digest, relative_path = line.split("  ", 1)
        actual_digest = hashlib.sha256((run_dir / relative_path).read_bytes()).hexdigest()
        assert actual_digest == expected_digest


def test_synthetic_repeat_selection_is_stable_and_includes_all_mandatory_symbols():
    symbols = tuple(f"{number:06d}" for number in range(1, 21))
    rows = {
        symbol: {
            "symbol": symbol,
            "row_outcome": "LIVE_FACT",
            "fact_status": "PARTIAL" if symbol == "000001" else "READY",
            "opening_fact": {"symbol": symbol},
            "field_status": {},
            "engine_status": "EXECUTED",
            "engine_error_type": None,
            "row_hash": "unused-in-selection-control",
        }
        for symbol in symbols
    }

    first = _select_repeat_symbols(
        symbols,
        rows,
        stale_symbols=("000002",),
        difference_symbols=("000003",),
        projection_content_hash="synthetic-projection-hash",
        sample_size=4,
    )
    second = _select_repeat_symbols(
        symbols,
        rows,
        stale_symbols=("000002",),
        difference_symbols=("000003",),
        projection_content_hash="synthetic-projection-hash",
        sample_size=4,
    )

    assert first == second
    assert {"000001", "000002", "000003"}.issubset(first["symbols"])
    assert len(first["sampled_symbols"]) == 4
    assert first["selection_rule"] == "MANDATORY_UNION_DETERMINISTIC_HASH_SAMPLE"


def test_checkpoint_resume_keeps_completed_batches_and_ignores_orphan_temp(tmp_path):
    symbols = ("000001", "000002", "000003")
    checkpoint_dir = tmp_path / "checkpoint" / "first_pass"
    manifest = {"fingerprint": {"q2_sha256": "pinned", "batch_size": 2}}
    rows = {
        symbol: {
            "symbol": symbol,
            "row_outcome": "LIVE_FACT",
            "opening_fact": {"symbol": symbol},
            "row_hash": "placeholder",
        }
        for symbol in symbols
    }
    # Checkpoint rows are content-addressed by the real helper, not this test label.
    for row in rows.values():
        row.pop("row_hash")
        row["row_hash"] = _row_hash(row)

    first = BatchCheckpoint(
        checkpoint_dir,
        phase="first_pass",
        manifest=manifest,
        expected_symbols=symbols,
        batch_size=2,
        python_hash_seed="1",
        resume=False,
    )
    first.commit_batch(
        0,
        symbols[:2],
        {symbol: rows[symbol] for symbol in symbols[:2]},
        metrics={
            "batch_elapsed_seconds": 2.5,
            "max_rss_kib": 123,
            "fallback_symbol_count": 2,
            "batch_error_types": ["builtins.ValueError"],
        },
    )
    (checkpoint_dir / ".batch-00001.json.tmp-interrupted").write_text("partial")

    resumed = BatchCheckpoint(
        checkpoint_dir,
        phase="first_pass",
        manifest=manifest,
        expected_symbols=symbols,
        batch_size=2,
        python_hash_seed="1",
        resume=True,
    )
    assert tuple(resumed.completed_rows) == symbols[:2]
    resumed.commit_batch(
        1,
        symbols[2:],
        {symbols[2]: rows[symbols[2]]},
        metrics={"batch_elapsed_seconds": 0.5, "max_rss_kib": 160},
    )
    assert tuple(resumed.completed_rows) == symbols
    assert resumed.batch_count == 2
    progress = checkpoint_dir.parent.parent / "first_pass_progress.json"
    assert progress.is_file()
    assert json.loads(progress.read_text())["completed_symbol_count"] == 3
    resumed.mark_complete()
    progress_payload = json.loads(progress.read_text())
    assert progress_payload["phase_status"] == "COMPLETE"
    assert progress_payload["fallback_symbol_count"] == 2
    assert progress_payload["batch_error_types"] == ["builtins.ValueError"]
    assert resumed.execution_summary()["active_batch_elapsed_seconds"] == 3.0
    assert resumed.execution_summary()["max_rss_kib"] == 160

    with pytest.raises(ValueError, match="manifest"):
        BatchCheckpoint(
            checkpoint_dir,
            phase="first_pass",
            manifest={"fingerprint": {"q2_sha256": "different", "batch_size": 2}},
            expected_symbols=symbols,
            batch_size=2,
            python_hash_seed="1",
            resume=True,
        )


def test_repeat_evidence_publication_is_idempotent_after_checksum_interruption(tmp_path):
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    report_path = output_dir / "final.json"
    summary_path = output_dir / "first_pass_summary.json"
    manifest_path = output_dir / "run_manifest.json"
    summary_path.write_text('{"summary":1}\n')
    manifest_path.write_text('{"manifest":1}\n')
    report = {"phase_status": "COMPLETE", "value": 42}

    _publish_final_evidence(report, report_path)
    first_bytes = report_path.read_bytes()
    (output_dir / "sha256sums.txt").unlink()
    _publish_final_evidence(report, report_path)

    assert report_path.read_bytes() == first_bytes
    assert (output_dir / "sha256sums.txt").is_file()
    assert json.loads(report_path.read_text()) == report

    with pytest.raises(ValueError, match="existing final report"):
        _publish_final_evidence({"phase_status": "COMPLETE", "value": 43}, report_path)
