from __future__ import annotations

import pytest

from engine_core import (
    OPENING_FACT_CONTRACT_VERSION,
    OPENING_AMOUNT_SUMMARY_CONTRACT_VERSION,
    OPENING_LIMIT_STATE_SUMMARY_CONTRACT_VERSION,
    OPENING_PLATE_AMOUNT_CONTEXT_CONTRACT_VERSION,
    OPENING_PLATE_AMOUNT_SUMMARY_CONTRACT_VERSION,
    OPENING_PLATE_PRICE_SUMMARY_CONTRACT_VERSION,
    OPENING_PLATE_PRICE_REFERENCE_CONTEXT_CONTRACT_VERSION,
    OPENING_TRANSITION_FACT_CONTRACT_VERSION,
    OPENING_TRANSITION_SUMMARY_CONTRACT_VERSION,
    build_open_fact,
    build_opening_amount_summary,
    build_opening_limit_state_summary,
    build_opening_plate_amount_context,
    build_opening_plate_amount_summary,
    build_opening_plate_price_summary,
    build_opening_plate_price_reference_context,
    build_opening_transition_fact,
    build_opening_transition_summary,
    classify_delta,
    classify_sign_state,
    validate_opening_plate_amount_context,
    validate_opening_plate_price_reference_context,
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


def test_opening_plate_price_summary_uses_common_valid_price_cohort():
    summary = build_opening_plate_price_summary(
        {
            "A": {"status": "available", "change_pct": 2.0},
            "B": {"status": "available", "change_pct": -1.0},
            "C": {"status": "available", "change_pct": 0.0},
            "D": {"status": "available", "change_pct": 50.0},
            "NO_CHANGE": {"status": "available", "change_pct": None},
            "NO_PRICE": {"status": "unavailable", "change_pct": 99.0},
        },
        mapped_symbols_by_plate={"AI": ("A", "B", "C", "D", "NO_CHANGE", "NO_PRICE")},
        auction_symbols_by_plate={"AI": ("A", "B", "C", "NO_CHANGE", "NO_PRICE")},
        selected_plates=("AI",),
    )

    plate = summary["plates"][0]
    assert summary["contract"] == OPENING_PLATE_PRICE_SUMMARY_CONTRACT_VERSION
    assert summary["full_market_coverage"] == "UNPROVEN"
    assert summary["decision_status"] == "FACT_ONLY"
    assert plate["open_valid_count"] == 5
    assert plate["common_symbol_count"] == 5
    assert plate["comparison_valid_count"] == 4
    assert plate["price_change_value_count"] == 3
    assert plate["price_change_missing_count"] == 1
    assert plate["price_change_status"] == "partial"
    assert plate["open_up_count"] == 1
    assert plate["open_down_count"] == 1
    assert plate["open_flat_count"] == 1
    assert plate["open_positive_ratio"] == pytest.approx(1 / 3)
    assert plate["open_negative_ratio"] == pytest.approx(1 / 3)
    assert plate["open_median_change_pct"] == pytest.approx(0.0)
    assert plate["comparison_symbols"] == ["A", "B", "C", "NO_CHANGE"]


def test_opening_plate_price_summary_is_order_independent_and_keeps_empty_plate_visible():
    facts = {
        "A": {"status": "available", "change_pct": 1.0},
        "B": {"status": "available", "change_pct": -1.0},
    }
    kwargs = {
        "facts_by_symbol": facts,
        "mapped_symbols_by_plate": {"P": ("A", "B"), "EMPTY": ("C",)},
        "auction_symbols_by_plate": {"P": ("A", "B"), "EMPTY": ()},
        "selected_plates": ("P", "EMPTY"),
    }
    left = build_opening_plate_price_summary(**kwargs)
    right = build_opening_plate_price_summary(
        **{
            **kwargs,
            "facts_by_symbol": dict(reversed(tuple(facts.items()))),
            "mapped_symbols_by_plate": dict(reversed(tuple(kwargs["mapped_symbols_by_plate"].items()))),
            "auction_symbols_by_plate": dict(reversed(tuple(kwargs["auction_symbols_by_plate"].items()))),
            "selected_plates": ("EMPTY", "P"),
        }
    )

    assert left == right
    by_plate = {row["plate"]: row for row in left["plates"]}
    assert by_plate["P"]["open_median_change_pct"] == pytest.approx(0.0)
    assert by_plate["EMPTY"]["open_median_change_pct"] is None
    assert by_plate["EMPTY"]["price_change_status"] == "unavailable"


def test_opening_plate_price_summary_compares_only_explicit_auction_reference_facts():
    reference_context = build_opening_plate_price_reference_context(
        trade_date="2026-09-29",
        source_provenance={"trade_date": "2026-09-29", "source_sha256": "a" * 64},
        auction_price_stats_by_plate={
            "P": {"positive_ratio": 0.5, "median_change_pct": -1.0},
            "Q": {"positive_ratio": 2.0, "median_change_pct": None},
        },
        selected_plates=("P", "Q", "NOT_REPORTED"),
    )
    assert reference_context["contract"] == OPENING_PLATE_PRICE_REFERENCE_CONTEXT_CONTRACT_VERSION
    assert validate_opening_plate_price_reference_context(
        reference_context,
        trade_date="2026-09-29",
        selected_plates=("P", "Q", "NOT_REPORTED"),
    ) == reference_context
    assert reference_context["auction_price_stats_by_plate"]["P"] == {
        "auction_positive_ratio": 0.5,
        "auction_positive_ratio_status": "AVAILABLE",
        "auction_median_change_pct": -1.0,
        "auction_median_change_pct_status": "AVAILABLE",
    }
    assert reference_context["auction_price_stats_by_plate"]["Q"][
        "auction_positive_ratio_status"
    ] == "INVALID"
    assert reference_context["auction_price_stats_by_plate"]["NOT_REPORTED"][
        "auction_positive_ratio_status"
    ] == "NOT_REPORTED"

    summary = build_opening_plate_price_summary(
        {
            "A": {"status": "available", "change_pct": 2.0},
            "B": {"status": "available", "change_pct": 1.0},
            "C": {"status": "available", "change_pct": 3.0},
        },
        mapped_symbols_by_plate={"P": ("A", "B", "C")},
        auction_symbols_by_plate={"P": ("A", "B", "C")},
        selected_plates=("P",),
        auction_price_reference_by_plate=reference_context[
            "auction_price_stats_by_plate"
        ],
    )
    plate = summary["plates"][0]
    assert summary["contract"] == OPENING_PLATE_PRICE_SUMMARY_CONTRACT_VERSION
    assert plate["auction_positive_ratio"] == pytest.approx(0.5)
    assert plate["positive_ratio_delta"] == pytest.approx(0.5)
    assert plate["price_breadth_state"] == "expanded"
    assert plate["auction_median_change_pct"] == pytest.approx(-1.0)
    assert plate["median_change_pct_delta"] == pytest.approx(3.0)
    assert plate["median_change_state"] == "reversed"

    without_reference = build_opening_plate_price_summary(
        {"A": {"status": "available", "change_pct": 0.0}},
        mapped_symbols_by_plate={"P": ("A",)},
        auction_symbols_by_plate={"P": ("A",)},
        selected_plates=("P",),
    )["plates"][0]
    assert without_reference["auction_positive_ratio_status"] == "NOT_PROVIDED"
    assert without_reference["positive_ratio_delta"] is None
    assert without_reference["price_breadth_state"] == "unavailable"


def test_opening_plate_price_reference_context_is_date_and_cohort_pinned():
    context = build_opening_plate_price_reference_context(
        trade_date="2026-09-29",
        source_provenance={},
        auction_price_stats_by_plate={"P": {"positive_ratio": 0.0, "median_change_pct": 0.0}},
        selected_plates=("P",),
    )
    assert context["auction_price_stats_by_plate"]["P"][
        "auction_positive_ratio_status"
    ] == "AVAILABLE"
    with pytest.raises(ValueError, match="trade_date does not match"):
        validate_opening_plate_price_reference_context(
            context, trade_date="2026-09-30", selected_plates=("P",)
        )
    with pytest.raises(ValueError, match="selected_plates do not match"):
        validate_opening_plate_price_reference_context(
            context, trade_date="2026-09-29", selected_plates=("Q",)
        )


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


def test_opening_transition_summary_uses_observed_0925_anchors_without_zero_fill():
    auction_anchors = {
        "000001": {"status": "AVAILABLE", "price_milli": 12_000, "source_time_ms": 10},
        "000002": {"status": "MISSING", "price_milli": None, "source_time_ms": 10},
        "000003": {"status": "AVAILABLE", "price_milli": 9_000, "source_time_ms": 10},
    }
    opening_rows = {
        "000001": {
            "symbol": "000001",
            "timestamp_ms": 20,
            "price_milli": 10_200,
            "previous_close_milli": 10_000,
        },
        "000002": {
            "symbol": "000002",
            "timestamp_ms": 20,
            "price_milli": 10_100,
            "previous_close_milli": 10_000,
        },
        "000003": {
            "symbol": "000003",
            "timestamp_ms": 20,
            "price_milli": 10_000,
            "previous_close_milli": 10_000,
        },
        "000004": {
            "symbol": "000004",
            "timestamp_ms": 20,
            "price_milli": 10_000,
            "previous_close_milli": 10_000,
        },
    }

    summary = build_opening_transition_summary(
        auction_anchors,
        opening_rows,
        expected_symbols=("000001", "000002", "000003", "000004"),
    )

    assert summary["contract"] == OPENING_TRANSITION_SUMMARY_CONTRACT_VERSION
    assert summary["scope"] == "OBSERVED_COHORT"
    assert summary["expected_count"] == 4
    assert summary["auction_anchor_price_available_count"] == 2
    assert summary["opening_change_available_count"] == 4
    assert summary["transition_comparable_count"] == 2
    assert summary["transition_unavailable_count"] == 2
    assert summary["facts_by_symbol"]["000001"]["auction_change_pct"] == pytest.approx(20.0)
    assert summary["facts_by_symbol"]["000001"]["opening_change_pct"] == pytest.approx(2.0)
    assert summary["facts_by_symbol"]["000001"]["delta_change_pct"] == pytest.approx(-18.0)
    assert summary["facts_by_symbol"]["000001"]["auction_source_time_ms"] == 10
    assert summary["facts_by_symbol"]["000001"]["opening_source_time_ms"] == 20
    assert summary["facts_by_symbol"]["000002"]["auction_change_pct"] is None
    assert summary["facts_by_symbol"]["000002"]["status"] == "unavailable"
    assert summary["facts_by_symbol"]["000004"]["auction_change_pct"] is None
    assert summary["facts_by_symbol_hash"]
    assert summary["full_market_coverage"] == "UNPROVEN"


def test_opening_transition_summary_is_order_independent_and_rejects_out_of_cohort_rows():
    anchors = {
        "A": {"status": "AVAILABLE", "price_milli": 10_500, "source_time_ms": 10},
        "B": {"status": "AVAILABLE", "price_milli": 9_500, "source_time_ms": 10},
    }
    rows = {
        "A": {"symbol": "A", "timestamp_ms": 20, "price_milli": 10_200, "previous_close_milli": 10_000},
        "B": {"symbol": "B", "timestamp_ms": 20, "price_milli": 9_800, "previous_close_milli": 10_000},
    }

    left = build_opening_transition_summary(anchors, rows, expected_symbols=("A", "B"))
    right = build_opening_transition_summary(
        dict(reversed(tuple(anchors.items()))),
        dict(reversed(tuple(rows.items()))),
        expected_symbols=("B", "A"),
    )

    assert left == right
    with pytest.raises(ValueError, match="outside expected_symbols"):
        build_opening_transition_summary(anchors, rows, expected_symbols=("A",))
