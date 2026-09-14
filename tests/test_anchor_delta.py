from __future__ import annotations

import math
from datetime import datetime

import pytest

from examples.run_anchor_delta_shadow import build_shadow_from_td_rows, normalize_td_rows
from engine_core import (
    ANCHOR_DELTA_CONTRACT_VERSION,
    amount_reference_bucket,
    build_anchor_delta_evidence,
    build_anchor_shadow_evidence,
)


def _row(tag: str, *, price: object = 1305000, amount: object = 0, bid: object = 0, ask: object = 0):
    return {
        "symbol": "600519",
        "tag": tag,
        "price_milli": price,
        "auction_amount_yuan": amount,
        "bid_amount_yuan": bid,
        "ask_amount_yuan": ask,
    }


def test_anchor_delta_contract_matches_verified_legacy_facts_for_both_transitions():
    rows = [
        _row("0920", amount=10831500, bid=0, ask=2740500),
        _row("0924", amount=21532500, bid=5089500, ask=0),
        _row("0925", price=1305010, amount=33408300, bid=130500, ask=130501),
    ]

    first, second = (
        build_anchor_shadow_evidence(rows, from_tag=from_tag, to_tag=to_tag)[0]
        for from_tag, to_tag in (("0920", "0924"), ("0924", "0925"))
    )

    assert first == {
        "symbol": "600519",
        "from_anchor": "0920",
        "to_anchor": "0924",
        "amount_delta_yuan": 10701000.0,
        "price_delta_milli": 0.0,
        "rest_bid_delta_yuan": 5089500.0,
        "rest_ask_delta_yuan": -2740500.0,
        "pressure_delta_yuan": 7830000.0,
        "amount_ratio": 1.9879518072289155,
        "withdrawal_yuan": 0.0,
        "auction_directional_pressure_yuan": 10701000.0,
        "direction": "positive",
        "status": "resolved",
        "labels": ["buy_pressure_building"],
        "amount_reference_bucket": "gte_5m",
        "reference_labels": [],
    }
    assert second == {
        "symbol": "600519",
        "from_anchor": "0924",
        "to_anchor": "0925",
        "amount_delta_yuan": 11875800.0,
        "price_delta_milli": 10.0,
        "rest_bid_delta_yuan": -4959000.0,
        "rest_ask_delta_yuan": 130501.0,
        "pressure_delta_yuan": -5089501.0,
        "amount_ratio": 1.551529083942877,
        "withdrawal_yuan": 0.0,
        "auction_directional_pressure_yuan": 11875800.0,
        "direction": "positive",
        "status": "resolved",
        "labels": ["volume_price_strengthening"],
        "amount_reference_bucket": "gte_5m",
        "reference_labels": [],
    }
    assert ANCHOR_DELTA_CONTRACT_VERSION == "AnchorDeltaFactV1"


def test_anchor_delta_sorted_symbols_and_missing_anchor_are_fail_closed():
    rows = [
        {**_row("0925", amount=800000), "symbol": "000001"},
        {**_row("0924", amount=700000), "symbol": "000001"},
        {**_row("0924", amount=500000), "symbol": "600519"},
    ]
    result = build_anchor_shadow_evidence(rows, from_tag="0924", to_tag="0925")
    assert [item["symbol"] for item in result] == ["000001", "600519"]
    assert result[0]["status"] == "unresolved"
    assert result[1]["status"] == "unavailable"
    assert result[1]["from_anchor"] == "0924"
    assert result[1]["to_anchor"] == "0925"


@pytest.mark.parametrize(
    ("previous", "current", "expected_status"),
    [
        (_row("0924", price=None, amount=1), _row("0925", amount=2), "unavailable"),
        (_row("0924", price=-1, amount=1), _row("0925", amount=2), "invalid"),
        (_row("0924", price=0, amount=1), _row("0925", amount=2), "invalid"),
        (_row("0924", amount="nan"), _row("0925", amount=2), "unavailable"),
        (_row("0924", amount=1, ask=0), {**_row("0925", amount=2), "ask_amount_present": False}, "unavailable"),
    ],
)
def test_anchor_delta_missing_and_invalid_inputs_do_not_become_zero(previous, current, expected_status):
    result = build_anchor_delta_evidence(previous, current, symbol="600519")
    assert result["status"] == expected_status
    if expected_status != "resolved":
        assert result["amount_delta_yuan"] is None


def test_anchor_delta_supports_audited_aliases_and_balanced_state():
    previous = {"symbol": "600519", "tag": "0924", "price": 1305, "amount": 499999, "bid_amount": 10, "ask_amount": 10}
    current = {"symbol": "600519", "tag": "0925", "price": 1305, "amount": 499999, "bid_amount": 10, "ask_amount": 10}
    result = build_anchor_delta_evidence(previous, current, symbol="600519", from_tag="0924", to_tag="0925")
    assert result["status"] == "balanced"
    assert result["direction"] == "unresolved"
    assert result["labels"] == []
    assert result["amount_reference_bucket"] == "lt_500k"
    assert result["reference_labels"] == ["small_volume_unconfirmed"]


def test_td_adapter_keeps_native_timestamp_as_evidence_only():
    rows = [
        (datetime(2026, 9, 14, 9, 24, 10, 162000), 1305000, 0, 700000, 200000, 100000, 0, "600519", "20260914", "0924"),
        (datetime(2026, 9, 14, 9, 25, 6, 156000), 1305010, 0, 800000, 300000, 100000, 0, "600519", "20260914", "0925"),
    ]
    normalized = normalize_td_rows(rows)
    assert normalized[0]["source_record_time"] == datetime(2026, 9, 14, 9, 24, 10, 162000)
    result = build_shadow_from_td_rows(rows, from_tag="0924", to_tag="0925")
    assert result["facts"][0]["status"] == "resolved"
    assert result["read_only"] is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "lt_500k"),
        (499999.99, "lt_500k"),
        (500000, "500k_2m"),
        (1999999.99, "500k_2m"),
        (2000000, "2m_5m"),
        (4999999.99, "2m_5m"),
        (5000000, "gte_5m"),
        (math.inf, "invalid"),
    ],
)
def test_amount_reference_bucket_boundaries(value, expected):
    assert amount_reference_bucket(value) == expected
