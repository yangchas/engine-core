import importlib.util
import builtins
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "live_probe", Path(__file__).parents[1] / "examples" / "run_live_q2_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ReadClient:
    def __init__(self, active=(), row=None):
        self.active = active
        self.row = row or {}

    def smembers(self, key):
        return self.active

    def hgetall(self, key):
        return self.row


NOW = datetime(2026, 9, 9, 1, 32, tzinfo=timezone.utc)


def test_empty_universe_is_not_live_coverage_pass():
    result = probe.observe(ReadClient(), "2026-09-09", NOW, 60000)
    assert result["status"] == "MISSING"
    assert result["row_coverage"] == 0
    assert result["oldest_source_time_ms"] is None
    assert result["same_observation_engine_deterministic"]
    assert result["read_operation_counts"] == {"smembers": 2}
    assert result["raw_field_presence_counts"]["px"] == 0


def test_live_probe_requires_strict_trade_date():
    with pytest.raises(ValueError, match="strict"):
        probe._date_text("2026-9-9")


def test_real_zero_preserved_and_required_missing_reported():
    row = {"px": "1000", "pc": "1000", "amt": "0", "ts": str(int(NOW.timestamp()*1000))}
    capture = probe.ReadOnlyCapture(ReadClient(("000001",), row))
    projection = probe.RedisQ2ProjectionAdapter(capture).read("2026-09-09", NOW,
        freshness_policy=probe.FreshnessPolicy(stale_after_ms=60000))
    result = probe.observe(ReadClient(("000001",), row), "2026-09-09", NOW, 60000)
    assert not result["field_error_counts"]
    assert projection.quotes["000001"].amount_yuan == 0
    assert result["raw_field_presence_counts"]["amt"] == 1
    assert result["raw_field_explicit_zero_counts"]["amt"] == 1
    assert result["raw_value_counts"]["ls"] == {"": 1}
    assert result["same_observation_engine_deterministic"]
    assert capture.reads[-1]["value"]["amt"] == "0"
    del row["amt"]
    missing = probe.observe(ReadClient(("000001",), row), "2026-09-09", NOW, 60000)
    assert missing["field_error_counts"]["amt"] == 1
    assert missing["status"] == "PARTIAL"


def test_old_quote_not_promoted_to_today():
    row = {"px": "1000", "pc": "1000", "amt": "0", "ts": str(int(NOW.timestamp()*1000)-86400000)}
    result = probe.observe(ReadClient(("000001",), row), "2026-09-09", NOW, 60000)
    assert result["field_error_counts"]["stale"] == 1
    assert result["status"] != "READY"
    assert result["universe_authority"] == "NOT_PROVEN_BY_ACTIVE_SET"


def test_live_observation_uses_read_completion_time_and_states_its_scope(monkeypatch):
    row = {"px": "1000", "pc": "1000", "amt": "0", "ts": str(int(NOW.timestamp()*1000))}

    fixed_start = (
        datetime.now(timezone.utc).replace(microsecond=0)
        - timedelta(milliseconds=70)
        + timedelta(microseconds=7)
    )

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_start

    monkeypatch.setattr(probe, "datetime", FixedDateTime)
    result = probe.observe(ReadClient(("000001",), row), "2026-09-09", None, None)

    started = datetime.fromisoformat(result["read_started_at"])
    completed = datetime.fromisoformat(result["read_completed_at"])
    assert started.microsecond % 1000 == 0
    assert started <= completed
    assert result["observed_at"] == result["read_completed_at"]
    assert result["observation_time_mode"] == "LIVE_READ_COMPLETION"
    assert result["freshness_policy_stale_after_ms"] is None
    assert result["universe_authority"] == "NOT_PROVEN_BY_ACTIVE_SET"
    assert "does not prove full-market coverage" in result["status_scope"]


def test_source_record_age_preserves_future_timestamp_direction():
    row = {"px": "1000", "pc": "1000", "amt": "0", "ts": str(int(NOW.timestamp()*1000)+5000)}
    result = probe.observe(ReadClient(("000001",), row), "2026-09-09", NOW, None)

    assert result["newest_source_record_age_seconds"] == -5.0
    assert result["field_error_counts"]["future_ts"] == 1


def test_volume_unit_diagnostic_exposes_both_hypotheses_without_deciding_contract():
    row = {
        "px": "12000",
        "pc": "11900",
        "amt": "60000000",
        "vol": "50000",
        "ts": str(int(NOW.timestamp() * 1000)),
    }
    result = probe.observe(
        ReadClient(("000001",), row),
        "2026-09-09",
        NOW,
        60000,
        ("000001",),
    )
    diagnostic = result["volume_unit_diagnostics"][0]
    assert diagnostic["current_price_yuan"] == 12.0
    assert diagnostic["implied_average_price_yuan_if_lots"] == 12.0
    assert diagnostic["implied_average_price_yuan_if_shares"] == 1200.0
    assert "volume_unit" not in diagnostic


def test_q2_read_capture_can_replay_the_exact_observed_redis_input():
    row = {
        "px": "1000",
        "pc": "990",
        "amt": "0",
        "vol": "0",
        "ts": str(int(NOW.timestamp() * 1000)),
    }
    live = probe.observe(
        ReadClient(("000001",), row),
        "2026-09-09",
        NOW,
        60000,
        ("000001", "300750", "600519"),
        include_raw_capture=True,
    )
    reads = live.pop("_raw_read_capture")
    artifact = probe.build_q2_read_capture_artifact("2026-09-09", live, reads)

    assert artifact["contract_version"] == "RedisQ2ReadCaptureV1"
    assert artifact["input_canonical_sha256"] == live["input_canonical_sha256"]
    assert artifact["reads"] == reads
    assert artifact["freshness_policy_stale_after_ms"] == 60000
    assert artifact["volume_unit_diagnostic_symbols"] == [
        "000001",
        "300750",
        "600519",
    ]
    assert all(item["operation"] in {"smembers", "hgetall"} for item in reads)
    assert "redis_password" not in str(artifact).lower()

    replay_client = probe.CapturedQ2ReadClient(artifact)
    replayed = probe.observe(
        replay_client,
        "2026-09-09",
        datetime.fromisoformat(artifact["read_completed_at"]),
        60000,
        tuple(artifact["volume_unit_diagnostic_symbols"]),
    )
    replay_client.assert_consumed()

    assert replayed["input_canonical_sha256"] == live["input_canonical_sha256"]
    assert replayed["projection_hash"] == live["projection_hash"]
    assert replayed["engine_run1"] == live["engine_run1"]
    assert replayed["engine_run2"] == live["engine_run2"]
    assert replayed["volume_unit_diagnostics"] == live["volume_unit_diagnostics"]


def test_q2_read_capture_cli_replays_offline_without_importing_redis(
    monkeypatch, tmp_path, capsys
):
    row = {
        "px": "12000",
        "pc": "11900",
        "amt": "60000000",
        "vol": "50000",
        "ts": str(int(NOW.timestamp() * 1000)),
    }
    live = probe.observe(
        ReadClient(("000001",), row),
        "2026-09-09",
        NOW,
        60000,
        ("000001",),
        include_raw_capture=True,
    )
    reads = live.pop("_raw_read_capture")
    artifact = probe.build_q2_read_capture_artifact("2026-09-09", live, reads)
    capture_path = tmp_path / "q2-read-capture.json"
    capture_path.write_text(json.dumps(artifact), encoding="utf-8")
    output_path = tmp_path / "offline-replay-report.json"
    real_import = builtins.__import__

    def reject_redis_import(name, *args, **kwargs):
        if name == "redis":
            raise AssertionError("offline replay attempted to import Redis")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_redis_import)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_live_q2_probe.py",
            "--trade-date",
            "2026-09-09",
            "--replay-input",
            str(capture_path),
            "--output",
            str(output_path),
        ],
    )

    probe.main()

    stdout_report = json.loads(capsys.readouterr().out)
    stored_report = json.loads(output_path.read_text(encoding="utf-8"))
    assert stdout_report["projection_hash"] == live["projection_hash"]
    assert stdout_report["engine_run1"] == live["engine_run1"]
    assert stdout_report["volume_unit_diagnostics"] == live["volume_unit_diagnostics"]
    assert stdout_report["captured_source_observation"]["capture_input_sha256"] == (
        live["input_canonical_sha256"]
    )
    assert stored_report["input_canonical_sha256"] == live["input_canonical_sha256"]
    assert "reads" not in stored_report
    assert output_path.with_name(output_path.name + ".sha256").exists()


def test_q2_read_capture_rejects_tampered_payload_or_non_read_operation():
    row = {"px": "1000", "pc": "990", "amt": "0", "ts": str(int(NOW.timestamp() * 1000))}
    live = probe.observe(
        ReadClient(("000001",), row),
        "2026-09-09",
        NOW,
        60000,
        include_raw_capture=True,
    )
    reads = live.pop("_raw_read_capture")
    artifact = probe.build_q2_read_capture_artifact("2026-09-09", live, reads)

    tampered_reads = [dict(item) for item in artifact["reads"]]
    tampered_reads[-1] = {
        **tampered_reads[-1],
        "value": {**tampered_reads[-1]["value"], "px": "1"},
    }
    tampered = {**artifact, "reads": tampered_reads}
    with pytest.raises(ValueError, match="hash"):
        probe.CapturedQ2ReadClient(tampered)

    with pytest.raises(ValueError, match="read-only"):
        probe.build_q2_read_capture_artifact(
            "2026-09-09",
            live,
            [{"operation": "set", "key": "x", "value": "y"}],
        )
