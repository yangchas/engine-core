from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine_core import (
    AuctionMarketSummaryFact,
    FactStatus,
    normalize_auction_market_summary,
)


CAPTURE = Path(__file__).parent.parent / "tmp/capture-20260914/auction_0920.json"


def _raw_summary() -> dict:
    payload = json.loads(CAPTURE.read_text(encoding="utf-8"))
    return json.loads(payload["hash_fields"]["summary"])


def test_real_capture_summary_maps_to_canonical_a2_fact():
    fact = normalize_auction_market_summary(
        _raw_summary(),
        trade_date="2026-09-14",
        source_id="fixture://capture-20260914/auction_0920",
        observation_time_ms=1789348803146,
        evidence_refs=("fixture://capture-20260914/auction_0920",),
    )
    assert isinstance(fact, AuctionMarketSummaryFact)
    assert fact.status is FactStatus.READY
    assert fact.stock_count == 4914
    assert fact.valid_stock_count == 1022
    assert fact.positive_count == 264
    assert fact.negative_count == 614
    assert fact.auction_amount_yuan == 2528071637
    assert fact.limit_up_seal_amount_yuan == 2743276692
    assert fact.missing_fields == ()
    assert fact.content_hash and fact.evidence_hash


def test_summary_preserves_explicit_zero_and_marks_missing_separately():
    raw = _raw_summary()
    raw["flat_open_count"] = 0
    raw.pop("limit_down_count")
    fact = normalize_auction_market_summary(
        raw,
        trade_date="2026-09-14",
        source_id="fixture://summary",
    )
    assert fact.status is FactStatus.PARTIAL
    assert fact.flat_count == 0
    assert fact.limit_down_count is None
    assert fact.missing_fields == ("limit_down_count",)


def test_summary_invalid_value_is_not_coerced_to_zero():
    raw = _raw_summary()
    raw["total_stocks"] = "not-a-count"
    fact = normalize_auction_market_summary(
        raw,
        trade_date="2026-09-14",
        source_id="fixture://summary",
    )
    assert fact.status is FactStatus.INVALID
    assert fact.stock_count is None
    assert fact.invalid_fields == ("stock_count",)


def test_empty_summary_is_unavailable():
    fact = normalize_auction_market_summary(
        {}, trade_date="2026-09-14", source_id="fixture://empty"
    )
    assert fact.status is FactStatus.UNAVAILABLE
    assert len(fact.missing_fields) == 10


def test_summary_semantic_hash_excludes_source_observation_evidence():
    raw = _raw_summary()
    left = normalize_auction_market_summary(
        raw, trade_date="2026-09-14", source_id="redis:a", observation_time_ms=1
    )
    right = normalize_auction_market_summary(
        raw, trade_date="2026-09-14", source_id="redis:b", observation_time_ms=1
    )
    assert left.content_hash == right.content_hash
    assert left.evidence_hash != right.evidence_hash


def test_summary_rejects_bad_date_and_timestamp():
    with pytest.raises(ValueError):
        normalize_auction_market_summary(
            _raw_summary(), trade_date="2026-02-30", source_id="fixture://bad"
        )
    with pytest.raises(ValueError):
        normalize_auction_market_summary(
            _raw_summary(), trade_date="2026-09-14", source_id="fixture://bad",
            observation_time_ms=0,
        )
