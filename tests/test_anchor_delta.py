from __future__ import annotations

import math
from datetime import datetime

import pytest

from examples.run_anchor_delta_shadow import build_shadow_from_td_rows, normalize_td_rows
from engine_core import (
    ANCHOR_DELTA_CONTRACT_VERSION,
    ANCHOR_FIELD_DELTA_CONTRACT_VERSION,
    amount_reference_bucket,
    build_anchor_delta_evidence,
    build_anchor_field_delta_evidence,
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


def test_anchor_field_delta_keeps_independent_amount_and_book_facts_when_price_is_missing():
    previous = {
        "symbol": "300170",
        "tag": "0924",
        "px_milli": 15570,
        "match_amt_yuan": 1_753_182,
        "rest_bid_amt_yuan": 0,
        "rest_ask_amt_yuan": 390_807,
    }
    current = {
        "symbol": "300170",
        "tag": "0925",
        "px_milli": None,
        "match_amt_yuan": 6_640_200,
        "rest_bid_amt_yuan": 0,
        "rest_ask_amt_yuan": 4_160_200,
    }

    result = build_anchor_field_delta_evidence(
        previous, current, symbol="300170", from_tag="0924", to_tag="0925"
    )
    legacy = build_anchor_delta_evidence(
        {
            "symbol": "300170",
            "tag": "0924",
            "price_milli": 15570,
            "auction_amount_yuan": 1_753_182,
            "bid_amount_yuan": 0,
            "ask_amount_yuan": 390_807,
        },
        {
            "symbol": "300170",
            "tag": "0925",
            "price_milli": None,
            "auction_amount_yuan": 6_640_200,
            "bid_amount_yuan": 0,
            "ask_amount_yuan": 4_160_200,
        },
        symbol="300170",
        from_tag="0924",
        to_tag="0925",
    )

    assert result["contract"] == "AnchorFieldDeltaFactV1"
    assert ANCHOR_FIELD_DELTA_CONTRACT_VERSION == "AnchorFieldDeltaFactV1"
    assert result["decision_status"] == "FACT_ONLY"
    assert legacy["status"] == "unavailable"
    assert legacy["amount_delta_yuan"] is None
    assert result["fields"]["price_milli"] == {
        "unit": "milli_yuan_per_share",
        "previous_value": 15570.0,
        "current_value": None,
        "delta": None,
        "status": "MISSING",
    }
    assert result["fields"]["amount_yuan"]["delta"] == 4_887_018.0
    assert result["fields"]["amount_yuan"]["status"] == "AVAILABLE"
    assert result["fields"]["rest_bid_yuan"]["delta"] == 0.0
    assert result["fields"]["rest_ask_yuan"]["delta"] == 3_769_393.0
    assert result["fields"]["book_pressure_yuan"]["delta"] == -3_769_393.0
    assert result["fields"]["book_pressure_yuan"]["status"] == "AVAILABLE"
    assert "direction" not in result

    complete_new = build_anchor_field_delta_evidence(
        previous,
        {**current, "px_milli": 15600},
        symbol="300170",
        from_tag="0924",
        to_tag="0925",
    )
    complete_old = build_anchor_delta_evidence(
        {
            "symbol": "300170",
            "tag": "0924",
            "price_milli": 15570,
            "auction_amount_yuan": 1_753_182,
            "bid_amount_yuan": 0,
            "ask_amount_yuan": 390_807,
        },
        {
            "symbol": "300170",
            "tag": "0925",
            "price_milli": 15600,
            "auction_amount_yuan": 6_640_200,
            "bid_amount_yuan": 0,
            "ask_amount_yuan": 4_160_200,
        },
        symbol="300170",
        from_tag="0924",
        to_tag="0925",
    )
    assert (
        complete_new["fields"]["price_milli"]["delta"]
        == complete_old["price_delta_milli"]
    )
    assert (
        complete_new["fields"]["amount_yuan"]["delta"]
        == complete_old["amount_delta_yuan"]
    )
    assert (
        complete_new["fields"]["rest_bid_yuan"]["delta"]
        == complete_old["rest_bid_delta_yuan"]
    )
    assert (
        complete_new["fields"]["rest_ask_yuan"]["delta"]
        == complete_old["rest_ask_delta_yuan"]
    )
    assert (
        complete_new["fields"]["book_pressure_yuan"]["delta"]
        == complete_old["pressure_delta_yuan"]
    )


def test_anchor_field_delta_distinguishes_null_unknown_and_invalid_without_zero_fill():
    previous = {
        "symbol": "600519",
        "tag": "0924",
        "price_milli": 10000,
        "auction_amount_yuan": 100,
        "bid_amount_yuan": 10,
        "ask_amount_yuan": 20,
    }
    current = {
        "symbol": "600519",
        "tag": "0925",
        "price_milli": None,
        "bid_amount_yuan": 0,
        "ask_amount_yuan": 0,
    }

    result = build_anchor_field_delta_evidence(previous, current, symbol="600519")

    assert result["fields"]["price_milli"]["status"] == "MISSING"
    assert result["fields"]["amount_yuan"]["status"] == "UNKNOWN"
    assert result["fields"]["amount_yuan"]["delta"] is None
    assert result["fields"]["rest_bid_yuan"]["status"] == "AVAILABLE"
    assert result["fields"]["rest_bid_yuan"]["delta"] == -10.0

    invalid = build_anchor_field_delta_evidence(
        {**previous, "auction_amount_yuan": -1},
        {**current, "auction_amount_yuan": 10},
        symbol="600519",
    )
    assert invalid["fields"]["amount_yuan"]["status"] == "INVALID"
    assert invalid["fields"]["amount_yuan"]["delta"] is None


def test_anchor_field_delta_zero_is_valid_and_identical_results_have_stable_hashes():
    previous = _row("0924", price=10000, amount=0, bid=0, ask=0)
    current = _row("0925", price=10000, amount=0, bid=0, ask=0)

    left = build_anchor_field_delta_evidence(previous, current, symbol="600519")
    right = build_anchor_field_delta_evidence(previous, current, symbol="600519")

    assert left["fields"]["amount_yuan"]["status"] == "AVAILABLE"
    assert left["fields"]["amount_yuan"]["delta"] == 0.0
    assert left["content_hash"] == right["content_hash"]


def test_anchor_field_delta_supports_yuan_price_alias_and_marks_zero_price_invalid():
    result = build_anchor_field_delta_evidence(
        {
            "symbol": "600519",
            "tag": "0924",
            "price": 100.0,
            "amount": 10,
            "bid_amount": 3,
            "ask_amount": 2,
        },
        {
            "symbol": "600519",
            "tag": "0925",
            "price": 100.1,
            "amount": 11,
            "bid_amount": 4,
            "ask_amount": 2,
        },
        symbol="600519",
    )

    assert result["fields"]["price_milli"]["delta"] == pytest.approx(100.0)
    assert result["fields"]["price_milli"]["status"] == "AVAILABLE"

    invalid = build_anchor_field_delta_evidence(
        _row("0924", price=10000), _row("0925", price=0), symbol="600519"
    )
    assert invalid["fields"]["price_milli"]["status"] == "INVALID"
    assert invalid["fields"]["price_milli"]["delta"] is None


def test_anchor_field_delta_missing_anchor_is_unknown_not_source_missing():
    result = build_anchor_field_delta_evidence(
        None,
        {
            "symbol": "600519",
            "tag": "0925",
            "px_milli": 10000,
            "match_amt_yuan": 0,
            "rest_bid_amt_yuan": 0,
            "rest_ask_amt_yuan": 0,
        },
        symbol="600519",
    )

    for name in ("price_milli", "amount_yuan", "rest_bid_yuan", "rest_ask_yuan"):
        assert result["fields"][name]["status"] == "UNKNOWN"
        assert result["fields"][name]["delta"] is None
    assert result["fields"]["book_pressure_yuan"]["status"] == "UNKNOWN"


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (None, _row("0925", price=10000, amount=100, bid=10, ask=20)),
        (_row("0924", price=10000, amount=100, bid=10, ask=20), None),
    ],
    ids=["previous-row-not-captured", "current-row-not-captured"],
)
def test_anchor_field_delta_absent_source_row_degrades_only_to_unknown(previous, current):
    result = build_anchor_field_delta_evidence(previous, current, symbol="600519")

    for name in (
        "price_milli",
        "amount_yuan",
        "rest_bid_yuan",
        "rest_ask_yuan",
        "book_pressure_yuan",
    ):
        assert result["fields"][name]["status"] == "UNKNOWN"
        assert result["fields"][name]["delta"] is None


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


def test_td_adapter_falls_back_when_legacy_alias_is_present_but_null():
    row = {
        "auction_tag": "0924",
        "symbol": "600519",
        "px_milli": None,
        "price_milli": 1305000,
        "match_amt_yuan": None,
        "auction_amount_yuan": 700000,
        "rest_bid_amt_yuan": None,
        "bid_amount_yuan": 200000,
        "rest_ask_amt_yuan": None,
        "ask_amount_yuan": 100000,
    }
    normalized = normalize_td_rows([row])[0]
    assert normalized["price_milli"] == 1305000
    assert normalized["auction_amount_yuan"] == 700000
    assert normalized["bid_amount_yuan"] == 200000
    assert normalized["ask_amount_yuan"] == 100000
    assert normalized["ask_amount_present"] is True


def test_td_adapter_input_hash_is_independent_of_provider_row_order():
    left = [
        (datetime(2026, 9, 14, 9, 25, 6), 1305010, 0, 800000, 300000, 100000, 0, "600519", "20260914", "0925"),
        (datetime(2026, 9, 14, 9, 24, 10), 1305000, 0, 700000, 200000, 100000, 0, "600519", "20260914", "0924"),
    ]
    right = list(reversed(left))
    assert build_shadow_from_td_rows(left, from_tag="0924", to_tag="0925")["normalized_input_hash"] == build_shadow_from_td_rows(
        right, from_tag="0924", to_tag="0925"
    )["normalized_input_hash"]


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
