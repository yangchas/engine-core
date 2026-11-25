from __future__ import annotations

import pytest

from examples.run_task008_late_start_anchor_observation import capture_to_single_q2frame


def _capture() -> dict:
    return {
        "contract_version": "RedisQ2ReadCaptureV1",
        "trade_date": "2026-10-09",
        "freshness_policy_stale_after_ms": 30_000,
        "observation_time_mode": "SEALED_READ",
        "read_started_at": "2026-10-09T01:42:27+00:00",
        "read_completed_at": "2026-10-09T01:42:29+00:00",
        "reads": [
            {
                "key": "q2:active:20261009",
                "operation": "smembers",
                "value": ["000001", "600000"],
            },
            {
                "key": "q2:000001",
                "operation": "hgetall",
                "value": {
                    "mk": "sz",
                    "px": "11720",
                    "a20": "11780",
                    "a24": "11780",
                    "a25": "11780",
                    "ts": "1791509964123",
                },
            },
            {
                "key": "q2:999999",
                "operation": "hgetall",
                "value": {"px": "1000", "ts": "1791509966987"},
            },
        ],
    }


def test_capture_is_one_sorted_q2frame_without_rewriting_hash_values():
    frame, inventory = capture_to_single_q2frame(_capture())

    assert frame["version"] == "Q2FrameV1"
    assert frame["seq_no"] == 1
    assert frame["logical_ts_ms"] == 1_791_509_964_000
    assert [row["symbol"] for row in frame["q2_updates"]] == ["000001"]
    assert frame["q2_updates"][0]["a20"] == "11780"
    assert frame["q2_updates"][0]["ts"] == "1791509964123"
    assert inventory["active_member_count"] == 2
    assert inventory["active_member_with_hash_count"] == 1
    assert inventory["active_member_without_hash_count"] == 1
    assert inventory["hash_outside_active_samples"] == ["999999"]
    assert inventory["read_wall_time_policy"] == "OBSERVED_ONLY_NOT_HISTORICAL_AVAILABLE_AT"


def test_capture_requires_trade_date_scoped_active_set():
    capture = _capture()
    capture["reads"] = capture["reads"][1:]

    with pytest.raises(ValueError, match="active-set read"):
        capture_to_single_q2frame(capture)
