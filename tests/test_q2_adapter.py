from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

from engine_core.contracts import DataStatus, PayloadKind
from engine_core.q2 import (
    FreshnessPolicy,
    IncrementalQ2Projection,
    Q2_FIELD_CONTRACT,
    Q2_PROJECTION_CONTRACT_VERSION,
    RedisQ2ProjectionAdapter,
    build_q2_projection,
    classify_equity,
    normalize_q2,
    normalize_symbol,
    validate_q2,
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
    assert specs["a20"].canonical_name == "auction_anchor_0920_price_milli"
    assert specs["a24"].canonical_name == "auction_anchor_0924_price_milli"
    assert specs["a25"].canonical_name == "auction_anchor_0925_price_milli"


def test_q2_projection_contract_v2_carries_anchor_quality_without_relabeling_source():
    projection = build_q2_projection(
        "2026-09-04",
        datetime(2026, 9, 4, 1, 20, tzinfo=timezone.utc),
        ("000001",),
        {"000001": {"px": "12345", "a25": "0", "ts": "1788484800000"}},
    )

    assert projection.envelope.schema_version == 2
    assert projection.envelope.payload["contract"] == Q2_PROJECTION_CONTRACT_VERSION
    assert projection.envelope.provenance.source_schema == "Q2RedisHashV1"
    assert projection.quotes["000001"].to_mapping()["auction_anchor_field_quality"][
        "a25"
    ] == "MISSING"


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


def test_q2_adapter_preserves_t1_v2_auction_anchor_prices_and_zero_as_unavailable():
    quote = normalize_q2(
        "000001",
        {
            "px": "12345",
            "a20": "12000",
            "a24": "12100",
            "a25": "0",
        },
    )

    assert quote.price_milli == 12345
    assert quote.auction_anchor_0920_price_milli == 12000
    assert quote.auction_anchor_0924_price_milli == 12100
    assert quote.auction_anchor_0925_price_milli is None
    assert quote.raw_fields["a25"] == "0"
    assert quote.to_mapping()["auction_anchor_0920_price_milli"] == 12000
    assert quote.to_mapping()["auction_anchor_0924_price_milli"] == 12100
    assert quote.to_mapping()["auction_anchor_0925_price_milli"] is None
    assert quote.to_mapping()["auction_anchor_field_quality"] == {
        "a20": "PRESENT_VALUE",
        "a24": "PRESENT_VALUE",
        "a25": "MISSING",
    }


def test_real_q2frame_missing_0925_price_keeps_independent_auction_fields():
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/q2/q2frame_null_a25_live_rows_20260930.json"
        ).read_text(encoding="utf-8")
    )

    assert fixture["q2frame_sha256"] == (
        "10264d0a6b6251e0c757f2113fd41e8e9e0669fade4ba05a145b78340a886ec0"
    )
    assert fixture["trade_date"] == "2026-09-30"
    source = fixture["source_provenance"]
    assert source["jsonl_line_number"] == 598
    assert source["logical_ts_ms"] == 1790731497000
    assert source["seq_no"] == 598
    for raw in fixture["q2_updates"]:
        symbol = raw["symbol"]
        canonical_row = json.dumps(
            raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        assert hashlib.sha256(canonical_row).hexdigest() == source[
            "canonical_update_sha256_by_symbol"
        ][symbol]
        quote = normalize_q2(
            symbol,
            {key: value for key, value in raw.items() if key != "symbol"},
        )

        assert raw["a25"] == 0
        assert quote.auction_anchor_0925_price_milli is None
        assert quote.auction_anchor_field_quality["a25"] == "MISSING"
        assert quote.price_milli == raw["px"] > 0
        assert quote.auction_amount_yuan == raw["am"] > 0
        assert quote.auction_bid_amount_yuan == raw["br"]
        assert quote.auction_ask_amount_yuan == raw["ar"]
        assert quote.source_record_time_ms == raw["ts"]


def test_q2_anchor_field_quality_distinguishes_absent_invalid_and_present():
    quote = normalize_q2(
        "000001",
        {"a20": "12000", "a24": "bad", "a25": "-1"},
    )

    assert quote.to_mapping()["auction_anchor_field_quality"] == {
        "a20": "PRESENT_VALUE",
        "a24": "INVALID",
        "a25": "INVALID",
    }
    absent = normalize_q2("000001", {"px": "12345"})
    assert absent.to_mapping()["auction_anchor_field_quality"] == {
        "a20": "MISSING",
        "a24": "MISSING",
        "a25": "MISSING",
    }


def test_q2_adapter_rejects_negative_auction_anchor_price_without_coercing_it():
    quote = normalize_q2("000001", {"a24": "-1"})

    assert quote.auction_anchor_0924_price_milli is None
    assert quote.raw_fields["a24"] == "-1"
    assert "a24_non_positive" in quote.field_errors


def test_validate_q2_exposes_stale_and_future_issues_directly():
    quote = normalize_q2(
        "000001",
        {"mk": "sz", "px": "1000", "pc": "990", "amt": "1", "ts": "1788484800000"},
    )
    errors = validate_q2(
        quote,
        observed_at_ms=1788484800002,
        trade_date="2026-09-04",
        freshness_policy=FreshnessPolicy(stale_after_ms=1, max_future_skew_ms=0),
    )
    assert errors == ("stale",)
    future_errors = validate_q2(
        quote,
        observed_at_ms=1788484799000,
        trade_date="2026-09-04",
        freshness_policy=FreshnessPolicy(stale_after_ms=None, max_future_skew_ms=0),
    )
    assert "future_ts" in future_errors


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


def test_q2_adapter_captures_observed_time_after_live_reads():
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
    before = datetime.now(timezone.utc)
    result = RedisQ2ProjectionAdapter(redis).read("2026-09-04")
    after = datetime.now(timezone.utc)
    observed_ms = result.envelope.observed_time_ms
    assert int(before.timestamp() * 1000) <= observed_ms <= int(after.timestamp() * 1000)
    assert redis.read_keys == [
        "q2:active:20260904",
        "q2:000001",
    ]


def test_q2_adapter_rejects_invalid_symbol_and_naive_observation():
    with pytest.raises(ValueError):
        normalize_symbol("not-a-symbol")
    redis = FakeRedis({"q2:active:2026-09-04": set()}, {})
    with pytest.raises(ValueError):
        RedisQ2ProjectionAdapter(redis).read(
            "2026-09-04",
            datetime(2026, 9, 4, 1, 20),
        )


@pytest.mark.parametrize("trade_date", ["2026-9-4", "2026-09-4", "bad"])
def test_q2_projection_rejects_non_strict_trade_date_before_classifying_data(trade_date):
    redis = FakeRedis({"q2:active:" + trade_date: set()}, {})
    with pytest.raises(ValueError, match="strict valid YYYY-MM-DD"):
        RedisQ2ProjectionAdapter(redis).read(
            trade_date,
            datetime(2026, 9, 4, 1, 20, tzinfo=timezone.utc),
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


def test_incremental_q2_full_hash_matches_full_rebuild_across_updates():
    expected = ("000001", "000002")
    first = {
        "000001": {"px": "1000", "pc": "990", "amt": "1", "ts": "1788484799000"},
        "000002": {"px": "2000", "pc": "1990", "amt": "2", "ts": "1788484799000"},
    }
    second = dict(first)
    second["000001"] = {**first["000001"], "px": "1010", "ts": "1788484800000"}
    observed = datetime(2026, 9, 4, 1, 20, tzinfo=timezone.utc)
    incremental = IncrementalQ2Projection("2026-09-04", expected)
    incremental.build(observed, first, changed_symbols=expected, full_hash=True)
    actual = incremental.build(observed, second, changed_symbols=("000001",), full_hash=True)
    expected_projection = build_q2_projection("2026-09-04", observed, expected, second)
    assert actual.content_hash == expected_projection.content_hash
    assert actual.missing_symbols == expected_projection.missing_symbols
    assert actual.stale_symbols == expected_projection.stale_symbols
    assert actual.quotes["000001"].to_mapping() == expected_projection.quotes["000001"].to_mapping()


def test_incremental_q2_full_parity_preserves_missing_and_stale_semantics():
    expected = ("000001", "000002")
    raw = {
        "000001": {"px": "1000", "pc": "990", "amt": "1", "ts": "1788484799000"},
    }
    policy = FreshnessPolicy(stale_after_ms=1_000)
    first_observed = datetime(2026, 9, 4, 1, 20, tzinfo=timezone.utc)
    later_observed = first_observed + timedelta(seconds=2)
    incremental = IncrementalQ2Projection("2026-09-04", expected, freshness_policy=policy)
    incremental.build(first_observed, raw, changed_symbols=("000001",), full_hash=True)
    actual = incremental.build(later_observed, raw, changed_symbols=(), full_hash=True)
    expected_projection = build_q2_projection(
        "2026-09-04", later_observed, expected, raw, freshness_policy=policy
    )
    assert actual.content_hash == expected_projection.content_hash
    assert actual.missing_symbols == ("000002",)
    assert actual.stale_symbols == expected_projection.stale_symbols
    assert actual.quotes["000001"].to_mapping() == expected_projection.quotes["000001"].to_mapping()


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


def test_real_legacy_context_amount_mapping_keeps_q2_amount_semantics_and_source_priority():
    """The real probe closes only the raw ``am`` mapping, not projection parity.

    For two bounded symbols the old context's auction amount equals Redis Q2
    ``am``.  The 600519 divergence is retained as evidence that the legacy
    consumer can prefer a frozen auction projection over the current Q2 value;
    it must not be hidden by treating every amount field as interchangeable.
    """

    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/legacy/legacy_q2_amount_mapping_20260918.json"
        ).read_text(encoding="utf-8")
    )
    observed = datetime.fromisoformat(fixture["observed_at"])
    assert observed.tzinfo is not None
    for row in fixture["rows"]:
        quote = normalize_q2(
            row["symbol"],
            {
                "mk": "sz" if row["symbol"].startswith(("000", "001", "002")) else "sh",
                "px": str(row["raw_px"]),
                "pc": str(row["raw_pc"]),
                "amt": str(row["raw_amt"]),
                "am": str(row["raw_am"]),
                "ts": str(row["source_record_time_ms"]),
            },
        )
        current_pct = (quote.price_milli / quote.pre_close_milli) - 1.0
        assert current_pct == pytest.approx(row["legacy_current_pct"])
        assert quote.auction_amount_yuan == row["raw_am"]
        if row["comparison"] == "MATCH":
            assert quote.auction_amount_yuan == row["legacy_context_auction_amount"]
        else:
            assert quote.auction_amount_yuan != row["legacy_context_auction_amount"]
