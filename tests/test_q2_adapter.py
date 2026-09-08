from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from engine_core.contracts import DataStatus, PayloadKind
from engine_core.q2 import (
    FreshnessPolicy,
    Q2_FIELD_CONTRACT,
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


def test_q2_contract_keeps_verified_legacy_units_explicit():
    specs = {spec.raw_name: spec for spec in Q2_FIELD_CONTRACT}
    assert specs["amt"].unit == "yuan"
    assert specs["amt"].semantic == "cumulative trading amount"
    assert specs["vol"].unit == "lots"
    assert specs["am"].canonical_name == "auction_amount_yuan"
    assert specs["am"].semantic == "current auction matched amount"
    assert specs["br"].unit == "yuan"
    assert specs["ar"].unit == "yuan"
    assert specs["spd1m"].canonical_name == "speed_1m_bp"
    assert specs["spd1m"].unit == "basis_point"
    assert specs["amt2m"].canonical_name == "amount_2m_yuan"
    assert specs["amt5m"].canonical_name == "amount_5m_yuan"
    assert specs["vec3m"].canonical_name == "vector_3m_bp"
    assert specs["vec5m"].canonical_name == "vector_5m_bp"


def test_q2_adapter_preserves_verified_rolling_metrics():
    quote = normalize_q2(
        "000001",
        {
            "px": "1000",
            "pc": "990",
            "amt": "100000000",
            "ts": "1788484799000",
            "spd1m": "125",
            "amt2m": "18000000",
            "amt5m": "28000000",
            "vec3m": "250",
            "vec5m": "-120",
        },
    )
    assert quote.speed_1m_bp == 125
    assert quote.amount_2m_yuan == 18_000_000
    assert quote.amount_5m_yuan == 28_000_000
    assert quote.vector_3m_bp == 250
    assert quote.vector_5m_bp == -120
    assert quote.to_mapping()["amount_5m_yuan"] == 28_000_000
    assert quote.to_mapping()["vector_5m_bp"] == -120


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
    assert result.quotes["000001"].amount_yuan == 0.0
    assert result.quotes["000001"].price_milli == 1000


def test_q2_adapter_prefers_legacy_compact_active_date_key():
    redis = FakeRedis(
        {"q2:active:20260904": {"000001"}},
        {
            "q2:000001": {
                "px": "1000",
                "pc": "990",
                "amt": "1",
                "ts": "1788484799000",
            }
        },
    )
    result = RedisQ2ProjectionAdapter(redis).read(
        "2026-09-04",
        datetime(2026, 9, 4, 1, 20, tzinfo=timezone.utc),
    )
    assert result.expected_symbols == ("000001",)


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
    assert result.quotes["000001"].source_record_time_ms is None
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
    assert quote.amount_yuan is None
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


def test_q2_adapter_does_not_mark_missing_amount_ready():
    redis = FakeRedis(
        {"q2:active:2026-09-04": {"000001"}},
        {"q2:000001": {"px": "1000", "pc": "990", "ts": "1788484799000"}},
    )
    result = RedisQ2ProjectionAdapter(redis).read(
        "2026-09-04",
        datetime(2026, 9, 4, 1, 20, tzinfo=timezone.utc),
    )
    assert result.status is DataStatus.PARTIAL
    assert "amt" in result.quotes["000001"].field_errors


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


def test_production_ground_truth_fixture_normalizes_without_raw_field_coupling():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/q2/production_ground_truth_20260826_0930.json").read_text(
            encoding="utf-8"
        )
    )
    first = fixture["snapshots"][0]
    observed = datetime.fromisoformat(first["observed_at"])
    result = RedisQ2ProjectionAdapter(
        FakeRedis(
            {"q2:active:2026-08-26": set(first["fields"])},
            {"q2:" + symbol: fields for symbol, fields in first["fields"].items()},
        )
    ).read("2026-08-26", observed)
    assert result.status is DataStatus.READY
    assert result.quotes["000001"].price_milli == 11540
    assert result.quotes["000001"].amount_yuan == 10196700
    assert "raw_fields" not in result.quotes["000001"].to_mapping()


def test_q2_cohort_identity_includes_observation_time_but_semantic_content_does_not():
    raw = {"px": "1000", "pc": "990", "ts": "1788484799000"}
    first = RedisQ2ProjectionAdapter(
        FakeRedis({"q2:active:2026-09-04": {"000001"}}, {"q2:000001": raw})
    ).read("2026-09-04", datetime(2026, 9, 4, 1, 20, tzinfo=timezone.utc))
    second = RedisQ2ProjectionAdapter(
        FakeRedis({"q2:active:2026-09-04": {"000001"}}, {"q2:000001": raw})
    ).read("2026-09-04", datetime(2026, 9, 4, 1, 20, 1, tzinfo=timezone.utc))
    assert first.content_hash == second.content_hash
    assert first.envelope.envelope_id != second.envelope.envelope_id
