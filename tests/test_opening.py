from __future__ import annotations

import pytest

from engine_core import (
    OPENING_FACT_CONTRACT_VERSION,
    OPENING_AMOUNT_SUMMARY_CONTRACT_VERSION,
    OPENING_LIMIT_STATE_SUMMARY_CONTRACT_VERSION,
    OPENING_PLATE_AMOUNT_CONTEXT_CONTRACT_VERSION,
    OPENING_PLATE_AMOUNT_SUMMARY_CONTRACT_VERSION,
    OPENING_TRANSITION_FACT_CONTRACT_VERSION,
    build_open_fact,
    build_opening_amount_summary,
    build_opening_limit_state_summary,
    build_opening_plate_amount_context,
    build_opening_plate_amount_summary,
    build_opening_transition_fact,
    classify_delta,
    classify_sign_state,
    validate_opening_plate_amount_context,
    compute_change_delta_bp,
    compute_delta,
    compute_open_change_pct,
)


def test_open_change_pct_uses_percentage_points_and_positive_prices():
    assert compute_open_change_pct(10500, 10000) == pytest.approx(5.0)
    assert compute_open_change_pct(10000, 0) is None
    assert compute_open_change_pct(-1, 10000) is None
    assert compute_open_change_pct("bad", 10000) is None


@pytest.mark.parametrize(
    ("after", "before", "expected"),
    [(5, 2, 3.0), (2, 5, -3.0), (0, 0, 0.0), (None, 1, None)],
)
def test_compute_delta_matches_legacy_order(after, before, expected):
    assert compute_delta(after, before) == expected


def test_delta_and_sign_state_zero_is_not_reversal():
    assert classify_delta(0) == "unchanged"
    assert classify_sign_state(-1, 0) == "expanded"
    assert classify_sign_state(0, 1) == "expanded"
    assert classify_sign_state(-1, 1) == "reversed"
    assert classify_sign_state(None, 1) == "unavailable"


@pytest.mark.parametrize(
    ("opening", "auction", "expected"),
    [(5.0, 2.0, 300), (1.234, 1.0, 23), (None, 1.0, None)],
)
def test_change_delta_bp_matches_legacy_percent_to_bp_conversion(
    opening, auction, expected
):
    assert compute_change_delta_bp(opening, auction) == expected


def test_build_open_fact_keeps_independent_statuses_and_units():
    result = build_open_fact(
        {
            "symbol": "600519",
            "timestamp_ms": 1788996600123,
            "price_milli": 10500,
            "previous_close_milli": 10000,
            "amount_2m_yuan": 1200000,
            "limit_state": 1,
            "name": "fixture",
            "speed_1m": 2.5,
        }
    )
    assert result == {
        "symbol": "600519",
        "timestamp_ms": 1788996600123,
        "change_pct": pytest.approx(5.0),
        "amount_2m_yuan": 1200000.0,
        "limit_state": 1,
        "limit_state_status": "available",
        "name": "fixture",
        "speed_1m": 2.5,
        "status": "available",
    }
    assert OPENING_FACT_CONTRACT_VERSION == "OpeningFactV1"


def test_build_open_fact_does_not_infer_limit_state_from_change():
    result = build_open_fact(
        {
            "symbol": "000001",
            "price_milli": 12000,
            "previous_close_milli": 10000,
            "limit_state": "unknown",
        }
    )
    assert result["change_pct"] == pytest.approx(20.0)
    assert result["status"] == "available"
    assert result["limit_state"] == "unknown"
    assert result["limit_state_status"] == "invalid"


def test_opening_limit_state_summary_keeps_enum_counts_separate_from_price_breadth():
    summary = build_opening_limit_state_summary(
        {
            "UP": {"status": "available", "limit_state": 1},
            "NORMAL": {"status": "available", "limit_state": 0},
            "DOWN": {"status": "available", "limit_state": -1},
            # Price-ineligible rows are not in the legacy open comparison
            # denominator, even if they happen to carry an ls value.
            "NO_PRICE": {"status": "unavailable", "limit_state": 1},
        },
        expected_symbols=("UP", "NORMAL", "DOWN", "NO_PRICE", "NO_ROW"),
    )

    assert summary["contract"] == "OpeningLimitStateSummaryV1"
    assert summary["scope"] == "OBSERVED_COHORT"
    assert summary["full_market_coverage"] == "UNPROVEN"
    assert summary["expected_count"] == 5
    assert summary["observed_count"] == 4
    assert summary["missing_symbol_count"] == 1
    assert summary["symbol_coverage"] == pytest.approx(0.8)
    assert summary["price_eligible_count"] == 3
    assert summary["limit_state_total_count"] == 3
    assert summary["limit_state_present_count"] == 3
    assert summary["limit_state_valid_count"] == 3
    assert summary["limit_state_missing_count"] == 0
    assert summary["limit_state_invalid_count"] == 0
    assert summary["limit_state_counts"] == {
        "up_count": 1,
        "normal_count": 1,
        "down_count": 1,
    }
    assert summary["cohort_field_status"] == "available"
    assert summary["valid_coverage"] == 1.0
    assert summary["content_hash"]
    assert OPENING_LIMIT_STATE_SUMMARY_CONTRACT_VERSION == "OpeningLimitStateSummaryV1"


def test_opening_limit_state_summary_distinguishes_missing_invalid_and_known_values():
    summary = build_opening_limit_state_summary(
        {
            "UP": {"status": "available", "limit_state": 1},
            "MISSING": {"status": "available", "limit_state": None},
            "INVALID": {"status": "available", "limit_state": 2},
            "NO_PRICE": {"status": "unavailable", "limit_state": None},
        }
    )

    assert summary["observed_count"] == 4
    assert summary["price_eligible_count"] == 3
    assert summary["limit_state_total_count"] == 3
    assert summary["limit_state_present_count"] == 2
    assert summary["limit_state_valid_count"] == 1
    assert summary["limit_state_missing_count"] == 1
    assert summary["limit_state_invalid_count"] == 1
    assert summary["limit_state_counts"] == {
        "up_count": 1,
        "normal_count": 0,
        "down_count": 0,
    }
    assert summary["cohort_field_status"] == "partial"
    assert summary["valid_coverage"] == pytest.approx(1 / 3)


def test_opening_limit_state_summary_empty_or_all_unknown_is_unavailable_not_zero():
    empty = build_opening_limit_state_summary({})
    unknown = build_opening_limit_state_summary(
        {"A": {"status": "available", "limit_state": None}}
    )

    assert empty["cohort_field_status"] == "unavailable"
    assert empty["limit_state_total_count"] == 0
    assert empty["valid_coverage"] is None
    assert empty["limit_state_counts"] == {
        "up_count": 0,
        "normal_count": 0,
        "down_count": 0,
    }
    assert unknown["cohort_field_status"] == "unavailable"
    assert unknown["limit_state_missing_count"] == 1
    assert unknown["limit_state_counts"] == {
        "up_count": 0,
        "normal_count": 0,
        "down_count": 0,
    }


def test_opening_limit_state_summary_hash_is_order_independent_and_scope_explicit():
    first = {
        "A": {"status": "available", "limit_state": 1},
        "B": {"status": "available", "limit_state": 0},
    }
    reversed_order = dict(reversed(tuple(first.items())))

    left = build_opening_limit_state_summary(first, scope="FRESH_OBSERVED_COHORT")
    right = build_opening_limit_state_summary(
        reversed_order, scope="FRESH_OBSERVED_COHORT"
    )

    assert left == right
    assert left["scope"] == "FRESH_OBSERVED_COHORT"
    assert left["full_market_coverage"] == "UNPROVEN"


def test_opening_limit_state_summary_rejects_unverified_scope_labels():
    with pytest.raises(ValueError, match="scope"):
        build_opening_limit_state_summary(
            {"A": {"status": "available", "limit_state": 1}},
            scope="FULL_MARKET",
        )


def test_opening_amount_summary_matches_legacy_complete_sum_and_price_cohort():
    summary = build_opening_amount_summary(
        {
            "A": {"status": "available", "amount_2m_yuan": 0},
            "B": {"status": "available", "amount_2m_yuan": 50},
            # The legacy opening consumer excludes price-ineligible rows from
            # its market amount denominator, even if the amount is present.
            "NO_PRICE": {"status": "unavailable", "amount_2m_yuan": 900},
        },
        expected_symbols=("A", "B", "NO_PRICE", "NOT_OBSERVED"),
    )

    assert summary["contract"] == "OpeningAmountSummaryV1"
    assert summary["scope"] == "OBSERVED_COHORT"
    assert summary["scope_authority"] == "Q2_COHORT_ONLY_NOT_FULL_MARKET"
    assert summary["full_market_coverage"] == "UNPROVEN"
    assert summary["expected_count"] == 4
    assert summary["observed_count"] == 3
    assert summary["missing_symbol_count"] == 1
    assert summary["price_eligible_count"] == 2
    assert summary["amount_2m_yuan_total_count"] == 2
    assert summary["amount_2m_yuan_present_count"] == 2
    assert summary["amount_2m_yuan_missing_count"] == 0
    assert summary["amount_2m_yuan_sum"] == 50
    assert summary["amount_2m_yuan_status"] == "available"
    assert summary["content_hash"]
    assert OPENING_AMOUNT_SUMMARY_CONTRACT_VERSION == "OpeningAmountSummaryV1"


def test_opening_amount_summary_keeps_partial_and_missing_distinct_from_zero():
    summary = build_opening_amount_summary(
        {
            "ZERO": {"status": "available", "amount_2m_yuan": 0},
            "MISSING": {"status": "available", "amount_2m_yuan": None},
            "NO_PRICE": {"status": "unavailable", "amount_2m_yuan": 100},
        }
    )

    assert summary["price_eligible_count"] == 2
    assert summary["amount_2m_yuan_total_count"] == 2
    assert summary["amount_2m_yuan_present_count"] == 1
    assert summary["amount_2m_yuan_missing_count"] == 1
    assert summary["amount_2m_yuan_sum"] is None
    assert summary["amount_2m_yuan_status"] == "partial"
    assert summary["amount_2m_yuan_coverage"] == 0.5


def test_opening_amount_summary_is_unavailable_for_empty_or_all_missing_cohorts():
    empty = build_opening_amount_summary({})
    missing = build_opening_amount_summary(
        {"A": {"status": "available", "amount_2m_yuan": None}}
    )

    assert empty["amount_2m_yuan_sum"] is None
    assert empty["amount_2m_yuan_status"] == "unavailable"
    assert missing["amount_2m_yuan_sum"] is None
    assert missing["amount_2m_yuan_status"] == "unavailable"
    assert missing["amount_2m_yuan_missing_count"] == 1


def test_opening_amount_summary_hash_is_order_independent_and_scope_explicit():
    first = {
        "A": {"status": "available", "amount_2m_yuan": 10},
        "B": {"status": "available", "amount_2m_yuan": 20},
    }
    second = dict(reversed(tuple(first.items())))

    left = build_opening_amount_summary(first, scope="FRESH_OBSERVED_COHORT")
    right = build_opening_amount_summary(
        second, scope="FRESH_OBSERVED_COHORT"
    )

    assert left == right
    with pytest.raises(ValueError, match="observed Q2 cohort"):
        build_opening_amount_summary(first, scope="FULL_MARKET")


def test_opening_plate_amount_summary_keeps_open_and_common_denominators_separate():
    summary = build_opening_plate_amount_summary(
        {
            "A": {"status": "available", "amount_2m_yuan": 10},
            "B": {"status": "available", "amount_2m_yuan": 30},
            "C": {"status": "available", "amount_2m_yuan": 20},
            "NO_PRICE": {"status": "unavailable", "amount_2m_yuan": 900},
            "OUTSIDE": {"status": "available", "amount_2m_yuan": 500},
        },
        mapped_symbols_by_plate={"AI": ("A", "B", "C", "NO_PRICE")},
        auction_symbols_by_plate={"AI": ("A", "C")},
        auction_top1_amount_ratio_by_plate={"AI": 0.5},
        selected_plates=("AI",),
    )

    plate = summary["plates"][0]
    assert summary["contract"] == "OpeningPlateAmountSummaryV1"
    assert summary["scope_authority"] == "FROZEN_MAPPING_AND_OBSERVED_Q2_COHORT"
    assert summary["full_market_coverage"] == "UNPROVEN"
    assert plate["mapped_symbol_count"] == 4
    assert plate["auction_symbol_count"] == 2
    assert plate["open_valid_count"] == 3
    assert plate["common_symbol_count"] == 2
    assert plate["comparison_valid_count"] == 2
    assert plate["open_window_amount_yuan"] == 60
    assert plate["open_window_amount_status"] == "available"
    assert plate["open_amount_total_count"] == 3
    assert plate["comparison_amount_total_count"] == 2
    assert plate["open_top1_amount_ratio"] == pytest.approx(2 / 3)
    assert plate["open_top3_amount_ratio"] == pytest.approx(1)
    assert plate["auction_top1_amount_ratio"] == pytest.approx(0.5)
    assert plate["top1_amount_ratio_delta"] == pytest.approx(1 / 6)
    assert plate["concentration_state"] == "expanded"
    assert plate["open_symbols"] == ["A", "B", "C"]


def test_opening_plate_amount_summary_missing_is_not_zero_and_empty_is_unavailable():
    summary = build_opening_plate_amount_summary(
        {
            "ZERO": {"status": "available", "amount_2m_yuan": 0},
            "MISSING": {"status": "available", "amount_2m_yuan": None},
            "NO_PRICE": {"status": "unavailable", "amount_2m_yuan": 100},
        },
        mapped_symbols_by_plate={"PARTIAL": ("ZERO", "MISSING", "NO_PRICE"), "EMPTY": ()},
        auction_symbols_by_plate={"PARTIAL": ("ZERO", "MISSING")},
        auction_top1_amount_ratio_by_plate={"PARTIAL": 0.4},
        selected_plates=("PARTIAL", "EMPTY"),
    )

    partial, empty = summary["plates"]
    assert partial["open_window_amount_status"] == "partial"
    assert partial["open_window_amount_yuan"] is None
    assert partial["open_amount_present_count"] == 1
    assert partial["open_amount_total_count"] == 2
    assert partial["comparison_amount_status"] == "partial"
    assert partial["open_top1_amount_ratio"] is None
    assert partial["top1_amount_ratio_delta"] is None
    assert empty["open_window_amount_status"] == "unavailable"
    assert empty["comparison_amount_status"] == "unavailable"
    assert empty["open_top1_amount_ratio"] is None


def test_opening_plate_amount_summary_is_order_independent_and_keeps_zero_total_unavailable():
    args = {
        "facts_by_symbol": {
            "A": {"status": "available", "amount_2m_yuan": 0},
            "B": {"status": "available", "amount_2m_yuan": 0},
        },
        "mapped_symbols_by_plate": {"P": ("A", "B")},
        "auction_symbols_by_plate": {"P": ("A", "B")},
        "auction_top1_amount_ratio_by_plate": {"P": 0.2},
        "selected_plates": ("P",),
    }
    left = build_opening_plate_amount_summary(**args)
    reversed_args = {
        **args,
        "facts_by_symbol": dict(reversed(tuple(args["facts_by_symbol"].items()))),
        "mapped_symbols_by_plate": {"P": ("B", "A")},
        "auction_symbols_by_plate": {"P": ("B", "A")},
    }
    right = build_opening_plate_amount_summary(**reversed_args)

    assert left == right
    plate = left["plates"][0]
    assert plate["open_window_amount_yuan"] == 0
    assert plate["open_top1_amount_ratio"] is None
    assert plate["concentration_state"] == "unavailable"
    assert OPENING_PLATE_AMOUNT_SUMMARY_CONTRACT_VERSION == "OpeningPlateAmountSummaryV1"


def test_opening_plate_amount_context_is_versioned_stable_and_trade_date_pinned():
    context = build_opening_plate_amount_context(
        trade_date="2026-09-29",
        source_provenance={
            "mapping_snapshot_sha256": "a" * 64,
            "auction_rows_sha256": "b" * 64,
        },
        mapped_symbols_by_plate={"AI": ("600000", "000001", "600000")},
        auction_symbols_by_plate={"AI": ("000001",)},
        auction_top1_amount_ratio_by_plate={"AI": 0.5},
        selected_plates=("AI", "AI"),
    )

    validated = validate_opening_plate_amount_context(
        context, trade_date="2026-09-29"
    )
    assert context["contract"] == OPENING_PLATE_AMOUNT_CONTEXT_CONTRACT_VERSION
    assert context["mapped_symbols_by_plate"] == {"AI": ["000001", "600000"]}
    assert context["selected_plates"] == ["AI"]
    assert validated == context
    with pytest.raises(ValueError, match="trade_date"):
        validate_opening_plate_amount_context(context, trade_date="2026-09-30")


def test_opening_plate_amount_context_rejects_tampered_hash_and_invalid_date():
    context = build_opening_plate_amount_context(
        trade_date="2026-09-29",
        source_provenance={},
        mapped_symbols_by_plate={},
        auction_symbols_by_plate={},
        auction_top1_amount_ratio_by_plate={},
        selected_plates=(),
    )
    tampered = {**context, "selected_plates": ["FORGED"]}
    with pytest.raises(ValueError, match="content_hash"):
        validate_opening_plate_amount_context(tampered, trade_date="2026-09-29")
    with pytest.raises(ValueError, match="trade_date"):
        build_opening_plate_amount_context(
            trade_date="2026-09-31",
            source_provenance={},
            mapped_symbols_by_plate={},
            auction_symbols_by_plate={},
            auction_top1_amount_ratio_by_plate={},
            selected_plates=(),
        )


def test_build_opening_transition_fact_is_fact_only_and_unit_explicit():
    result = build_opening_transition_fact(
        0.0,
        {
            "symbol": "600519",
            "price_milli": 1292000,
            "previous_close_milli": 1290880,
        },
    )
    assert result["auction_change_pct"] == 0.0
    assert result["opening_change_pct"] == pytest.approx(0.08676251859196515)
    assert result["delta_change_pct"] == pytest.approx(0.08676251859196515)
    assert result["delta_change_bp"] == 9
    assert result["delta_state"] == "expanded"
    assert result["sign_state"] == "expanded"
    assert result["status"] == "available"
    assert OPENING_TRANSITION_FACT_CONTRACT_VERSION == "OpeningTransitionFactV1"


@pytest.mark.parametrize(
    ("auction_change_pct", "opening_row"),
    [
        (None, {"symbol": "600519", "price_milli": 1292000, "previous_close_milli": 1290880}),
        (0.0, {"symbol": "600519", "price_milli": 0, "previous_close_milli": 1290880}),
    ],
)
def test_build_opening_transition_fact_does_not_fill_missing_delta(
    auction_change_pct, opening_row
):
    result = build_opening_transition_fact(auction_change_pct, opening_row)
    assert result["delta_change_pct"] is None
    assert result["delta_change_bp"] is None
    assert result["delta_state"] == "unavailable"
    assert result["sign_state"] == "unavailable"
    assert result["status"] == "unavailable"
