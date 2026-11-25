from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import examples.run_task008_arrival_lag_sensitivity as runner


def test_arrival_lag_summary_includes_td_fidelity_only_when_explicit(
    tmp_path: Path,
    monkeypatch,
):
    timestamp_ms = int(
        datetime(2026, 10, 9, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        * 1000
    )
    source = tmp_path / "one-frame.jsonl"
    source.write_text(
        json.dumps(
            {
                "version": "Q2FrameV1",
                "seq_no": 1,
                "logical_ts_ms": timestamp_ms,
                "phase": 1,
                "q2_updates": [
                    {
                        "symbol": "000001",
                        "ts": timestamp_ms,
                        "px": 10000,
                        "pc": 10000,
                        "amt": 100000,
                        "vol": 100,
                        "a20": 0,
                        "a24": 0,
                        "a25": 0,
                    }
                ],
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    expected_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    output_dir = tmp_path / "arrival-run"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_task008_arrival_lag_sensitivity.py",
            "--input",
            str(source),
            "--expected-sha256",
            expected_sha,
            "--trade-date",
            "2026-10-09",
            "--output-dir",
            str(output_dir),
            "--lags-ms",
            "0",
            "--cadences-ms",
            "1000",
            "--input-kind",
            "TD_RETAINED_EVENT_REPLAY",
        ],
    )

    assert runner.main() == 0
    summary = json.loads(
        (output_dir / "arrival_lag_sensitivity.json").read_text(encoding="utf-8")
    )
    assert summary["input_kind"] == "TD_RETAINED_EVENT_REPLAY"
    assert summary["contract"] == "ArrivalLagSensitivityRunV2"
    assert summary["replay_fidelity"]["contract"] == "TDRetainedEventReplayFidelityV1"
    assert summary["replay_fidelity"]["diagnostic_only"] is True
    assert summary["replay_fidelity"]["blocks_downstream"] is False
    assert summary["replay_fidelity"]["evidence_cohort_matches_run_input"][
        "status"
    ] == "DOES_NOT_MATCH"
