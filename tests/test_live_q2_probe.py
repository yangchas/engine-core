import importlib.util
from datetime import datetime, timezone
from pathlib import Path

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
    assert {r["operation"] for r in result["read_operations"]} == {"smembers"}


def test_real_zero_preserved_and_required_missing_reported():
    row = {"px": "1000", "pc": "1000", "amt": "0", "ts": str(int(NOW.timestamp()*1000))}
    result = probe.observe(ReadClient(("000001",), row), "2026-09-09", NOW, 60000)
    assert not result["field_errors"]
    assert result["same_observation_engine_deterministic"]
    assert result["read_operations"][-1]["value"]["amt"] == "0"
    del row["amt"]
    missing = probe.observe(ReadClient(("000001",), row), "2026-09-09", NOW, 60000)
    assert missing["field_errors"]["000001"]
    assert missing["status"] == "PARTIAL"


def test_old_quote_not_promoted_to_today():
    row = {"px": "1000", "pc": "1000", "amt": "0", "ts": str(int(NOW.timestamp()*1000)-86400000)}
    result = probe.observe(ReadClient(("000001",), row), "2026-09-09", NOW, 60000)
    assert result["field_errors"]["000001"]
    assert result["status"] != "READY"
    assert result["universe_authority"] == "NOT_PROVEN_BY_ACTIVE_SET"
