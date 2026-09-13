from __future__ import annotations

from datetime import datetime, timezone

import pytest

from examples.run_real_opening_facts import build_real_opening_facts
from examples.run_real_opening_facts import _date_text


class FakeRedis:
    def __init__(self):
        self.reads = []

    def smembers(self, key):
        self.reads.append(("smembers", key))
        return {"600519"}

    def hgetall(self, key):
        self.reads.append(("hgetall", key))
        return {
            "ts": "1788484799000",
            "px": "10500",
            "pc": "10000",
            "amt": "1000000",
            "amt2m": "120000",
            "ls": "1",
            "spd1m": "25",
            "name": "fixture",
            "mk": "sh",
        }


def test_real_opening_fact_composition_is_read_only_and_unit_explicit():
    client = FakeRedis()
    result = build_real_opening_facts(
        client,
        trade_date="2026-09-04",
        symbols=("600519",),
        observed_at=datetime(2026, 9, 4, 1, 20, tzinfo=timezone.utc),
        stale_after_ms=None,
    )
    assert result["facts"]["600519"]["change_pct"] == pytest.approx(5.0)
    assert result["facts"]["600519"]["status"] == "available"
    assert result["facts"]["600519"]["limit_state_status"] == "available"
    assert result["read_only"] is True
    assert all(operation in {"smembers", "hgetall"} for operation, _ in client.reads)


def test_missing_q2_symbol_is_unavailable_not_zero_filled():
    client = FakeRedis()
    result = build_real_opening_facts(
        client,
        trade_date="2026-09-04",
        symbols=("000001",),
        observed_at=datetime(2026, 9, 4, 1, 20, tzinfo=timezone.utc),
        stale_after_ms=None,
    )
    assert result["facts"]["000001"]["status"] == "unavailable"
    assert result["facts"]["000001"]["reason"] == "symbol_not_in_q2_cohort"


def test_opening_runner_requires_strict_trade_date():
    with pytest.raises(ValueError, match="strict"):
        _date_text("2026-9-4")
