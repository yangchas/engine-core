from datetime import datetime, timedelta, timezone

import pytest

from engine_core.contracts import DataStatus, PayloadKind
from engine_core.q2 import RedisQ2ProjectionAdapter, normalize_symbol


class FakeRedis:
    def __init__(self, active, hashes):
        self.active = active
        self.hashes = hashes
        self.read_keys = []

    def smembers(self, key):
        self.read_keys.append(key)
        return self.active.get(key, set())

    def hgetall(self, key):
        self.read_keys.append(key)
        return self.hashes.get(key, {})


def test_q2_adapter_reports_projection_cohort_and_missing_symbol():
    redis = FakeRedis(
        {"q2:active:2026-09-04": {b"000002", b"000001"}},
        {
            "q2:000001": {
                b"px": b"1000",
                b"pc": b"990",
                b"ts": b"1788484799000",
                b"amt": b"0",
            }
        },
    )
    observed = datetime(2026, 9, 4, 1, 20, tzinfo=timezone.utc)
    result = RedisQ2ProjectionAdapter(redis).read(
        "2026-09-04",
        observed,
        stale_after_ms=10_000,
    )
    assert result.status is DataStatus.PARTIAL
    assert result.coverage == 0.5
    assert result.missing_symbols == ("000002",)
    assert result.envelope.payload_kind is PayloadKind.L2_PROJECTION_SNAPSHOT
    assert result.envelope.generation is None
    assert result.envelope.generation_kind == "OBSERVATION_COHORT"
    assert result.quotes["000001"].amount_native == 0.0
    assert result.quotes["000001"].price_milli == 1000


def test_q2_adapter_rejects_invalid_symbol_and_naive_observation():
    with pytest.raises(ValueError):
        normalize_symbol("not-a-symbol")
    redis = FakeRedis({"q2:active:2026-09-04": set()}, {})
    with pytest.raises(ValueError):
        RedisQ2ProjectionAdapter(redis).read(
            "2026-09-04",
            datetime(2026, 9, 4, 1, 20),
        )


def test_q2_adapter_marks_out_of_range_timestamp_as_field_error():
    redis = FakeRedis(
        {"q2:active:2026-09-04": {"000001"}},
        {"q2:000001": {"px": "1000", "pc": "990", "ts": "20260904091959"}},
    )
    result = RedisQ2ProjectionAdapter(redis).read(
        "2026-09-04",
        datetime(2026, 9, 4, 1, 20, tzinfo=timezone.utc),
    )
    assert result.status is DataStatus.PARTIAL
    assert result.quotes["000001"].source_timestamp_ms is None
    assert "ts" in result.quotes["000001"].field_errors


def test_q2_adapter_rejects_non_finite_numeric_fields_as_missing_with_error():
    redis = FakeRedis(
        {"q2:active:2026-09-04": {"000001"}},
        {"q2:000001": {"px": "NaN", "pc": "990", "amt": "Infinity"}},
    )
    result = RedisQ2ProjectionAdapter(redis).read(
        "2026-09-04",
        datetime(2026, 9, 4, 1, 20, tzinfo=timezone.utc),
    )
    quote = result.quotes["000001"]
    assert quote.price_milli is None
    assert quote.amount_native is None
    assert set(quote.field_errors) == {"amt", "px"}
