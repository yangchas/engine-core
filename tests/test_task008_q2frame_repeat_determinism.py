from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import examples.run_task008_t1v2_q2frame_replay as replay_tool
from examples.run_task008_t1v2_q2frame_replay import (
    _DETERMINISM_FIELDS,
    _compare_runs,
    _inventory,
    _replay_status,
    _run_once,
    main,
)


FIXTURE = Path(__file__).parent / "fixtures/replay/q2frame_600519_20260903.jsonl"


def test_q2frame_cli_replays_same_file_twice_and_reports_required_metrics(monkeypatch, tmp_path):
    output = tmp_path / "replay-report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_task008_t1v2_q2frame_replay",
            "--q2frame",
            str(FIXTURE),
            "--trade-date",
            "2026-09-03",
            "--output",
            str(output),
        ],
    )

    assert main() == 0
    report = json.loads(output.read_text(encoding="utf-8"))

    assert report["contract_version"] == "Task008T1V2Q2FrameReplayV2"
    assert report["replay_status"] == "REPLAY_READY_BOUNDED"
    assert report["deterministic"] is True
    assert all(report["determinism"].values())
    assert set(_DETERMINISM_FIELDS) <= set(report["determinism"])
    assert report["ordered"]["frame_hashes"] == report["repeat"]["frame_hashes"]
    assert report["ordered"]["projection_hashes"] == report["repeat"]["projection_hashes"]
    assert len(report["ordered"]["frame_hashes"]) == 3
    assert report["ordered"]["processed_signals"] == 3
    assert report["ordered"]["reducer_revision"] == 3
    assert report["ordered"]["virtual_clock"] == {
        "final_utc": "2026-09-03T01:24:10+00:00",
        "final_epoch_ms": 1788398650000,
        "final_monotonic_ns": 542000000000,
    }
    assert report["ordered"]["final_state_hash"]
    coverage = report["ordered"]["symbol_coverage"]
    assert coverage["basis"] == "Q2FRAME_UNIQUE_SYMBOLS_ONLY"
    assert coverage["coverage"] == 1.0
    assert [item["projection_coverage"] for item in coverage["per_frame"]] == [1.0, 1.0, 1.0]
    assert report["historical_available_at"] == "UNKNOWN_NOT_INFERRED"
    assert report["historical_available_at_policy"] == (
        "NOT_INFERRED_FROM_LOGICAL_TS_SOURCE_TS_OR_REPLAY_TIME"
    )


def test_cli_preserves_empty_frames_without_claiming_market_coverage(monkeypatch, tmp_path):
    artifact = tmp_path / "empty-q2frame.jsonl"
    artifact.write_text(
        "\n".join(
            json.dumps(
                {
                    "version": "Q2FrameV1",
                    "seq_no": sequence,
                    "logical_ts_ms": 1788398650000 + (sequence - 1) * 3000,
                    "q2_updates": [],
                }
            )
            for sequence in (1, 2)
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "empty-report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_task008_t1v2_q2frame_replay",
            "--q2frame",
            str(artifact),
            "--trade-date",
            "2026-09-03",
            "--output",
            str(output),
        ],
    )

    assert main() == 0
    report = json.loads(output.read_text(encoding="utf-8"))

    assert report["inventory"]["frame_count"] == 2
    assert report["inventory"]["update_count"] == 0
    assert report["inventory"]["empty_frame_count"] == 2
    assert report["inventory"]["non_empty_frame_count"] == 0
    assert report["inventory"]["symbol_count"] == 0
    assert report["ordered"]["processed_signals"] == 2
    assert report["ordered"]["reducer_revision"] == 2
    coverage = report["ordered"]["symbol_coverage"]
    assert coverage["status"] == "UNKNOWN_NO_UNIVERSE"
    assert coverage["coverage"] is None
    assert coverage["expected_symbol_count"] == 0
    assert [item["projection_status"] for item in coverage["per_frame"]] == [
        "MISSING",
        "MISSING",
    ]
    assert [item["projection_consistency_status"] for item in coverage["per_frame"]] == [
        "EMPTY_UNIVERSE",
        "EMPTY_UNIVERSE",
    ]
    assert report["deterministic"] is True


def test_tick_evidence_defines_symbol_coverage_denominator(monkeypatch, tmp_path):
    evidence = tmp_path / "tick-manifest.json"
    evidence.write_text(
        json.dumps({"trade_date": "2026-09-03", "expected_symbols": ["600519", "000001"]}),
        encoding="utf-8",
    )
    output = tmp_path / "replay-report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_task008_t1v2_q2frame_replay",
            "--q2frame",
            str(FIXTURE),
            "--trade-date",
            "2026-09-03",
            "--tick-evidence",
            str(evidence),
            "--output",
            str(output),
        ],
    )

    assert main() == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    coverage = report["ordered"]["symbol_coverage"]
    assert coverage["basis"] == "TICK_EVIDENCE_EXPECTED_SYMBOLS"
    assert coverage["expected_symbol_count"] == 2
    assert coverage["covered_symbols"] == ["600519"]
    assert coverage["missing_expected_symbols"] == ["000001"]
    assert coverage["coverage"] == 0.5
    assert [item["projection_coverage"] for item in coverage["per_frame"]] == [0.5, 0.5, 0.5]
    assert report["tick_q2_coverage"]["coverage"] == 0.5


def test_any_required_metric_mismatch_is_reported_as_non_deterministic():
    inventory = _inventory(FIXTURE)
    baseline = _run_once(
        FIXTURE,
        "2026-09-03",
        tuple(inventory["symbols"]),
        inventory,
        coverage_basis="Q2FRAME_UNIQUE_SYMBOLS_ONLY",
    )

    for field in _DETERMINISM_FIELDS:
        changed = copy.deepcopy(baseline)
        if field in {"frame_hashes", "projection_hashes"}:
            changed[field] = (*changed[field], "different-hash")
        elif field in {"processed_signals", "reducer_revision"}:
            changed[field] += 1
        elif field == "final_state_hash":
            changed[field] = "different-state-hash"
        elif field == "virtual_clock":
            changed[field]["final_monotonic_ns"] += 1
        elif field == "symbol_coverage":
            changed[field]["coverage"] = 0.0

        comparison = _compare_runs(baseline, changed, input_sha256_stable=True)
        assert comparison[field] is False
        assert _replay_status(comparison) == "REPLAY_NON_DETERMINISTIC"

    unstable_input = _compare_runs(baseline, baseline, input_sha256_stable=False)
    assert _replay_status(unstable_input) == "REPLAY_NON_DETERMINISTIC"


def test_cli_labels_a_repeat_mismatch(monkeypatch, tmp_path):
    output = tmp_path / "non-deterministic-report.json"
    original_run_once = replay_tool._run_once
    run_count = 0

    def divergent_run(*args, **kwargs):
        nonlocal run_count
        result = original_run_once(*args, **kwargs)
        run_count += 1
        if run_count == 2:
            result["final_state_hash"] = "different-state-hash"
        return result

    monkeypatch.setattr(replay_tool, "_run_once", divergent_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_task008_t1v2_q2frame_replay",
            "--q2frame",
            str(FIXTURE),
            "--trade-date",
            "2026-09-03",
            "--output",
            str(output),
        ],
    )

    assert main() == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["determinism"]["final_state_hash"] is False
    assert report["replay_status"] == "REPLAY_NON_DETERMINISTIC"
