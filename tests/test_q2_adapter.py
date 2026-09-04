from datetime import datetime, timedelta, timezone

import pytest

from engine_core.contracts import DataStatus, PayloadKind
from engine_core.q2 import (
    FreshnessPolicy,
    RedisQ2ProjectionAdapter,
    classify_equity,
    normalize_q2,
    normalize_symbol,
)


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
    assert set(quote.field_errors) == {"amt", "px", "ts"}


def test_q2_adapter_marks_missing_core_fields_and_stale_projection():
    redis = FakeRedis(
        {"q2:active:2026-09-04": {"000001", "000002"}},
        {
            "q2:000001": {"pc": "990", "ts": "1788484799000"},
            "q2:000002": {"px": "1000", "pc": "990", "ts": "1788480000000"},
        },
    )
    observed = datetime(2026, 9, 4, 1, 20, tzinfo=timezone.utc)
    result = RedisQ2ProjectionAdapter(redis).read(
        "2026-09-04",
        observed,
        freshness_policy=FreshnessPolicy(stale_after_ms=10_000),
    )
    assert result.status is DataStatus.PARTIAL
    assert "px" in result.quotes["000001"].field_errors
    assert result.stale_symbols == ("000002",)


def test_q2_contract_keeps_unknown_fields_for_evidence_but_not_business_mapping():
    quote = normalize_q2(
        "000001",
        {"px": "1000", "pc": "990", "ts": "1788484799000", "future_field": "x"},
    )
    assert "future_field" in quote.raw_fields
    assert "future_field" not in quote.to_mapping()


def test_classify_equity_uses_legacy_market_and_symbol_rules():
    assert classify_equity("000001", {"mk": "sz", "px": "1000"}) is True
    assert classify_equity("399001", {"mk": "sz", "px": "1000"}) is False
    assert classify_equity("688001", {"mk": "kc", "px": "1000"}) is True
    assert classify_equity("SH.600000", {"mk": "sh", "px": "1000"}) is True
