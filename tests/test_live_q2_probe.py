import importlib.util
from datetime import datetime, timezone
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
