from __future__ import annotations

import json
from pathlib import Path

from engine_core import (
    derive_q2_auction_summary,
    normalize_q2,
)


Q2_LIMIT_STATE_CAPTURE = (
    Path(__file__).parent
    / "fixtures/q2/q2frame_0925_real_limit_states_20260930.json"
)


def _captured_rows() -> list[dict]:
    capture = json.loads(Q2_LIMIT_STATE_CAPTURE.read_text(encoding="utf-8"))
    return [item["q2_update"] for item in capture["records"]]


def _quotes(rows: list[dict]):
    return {row["symbol"]: normalize_q2(row["symbol"], row) for row in rows}


def test_real_q2_limit_state_rows_form_observed_auction_candidate_cohort():
    quotes = _quotes(_captured_rows())

    fact = derive_q2_auction_summary(
        quotes,
        source_id="fixture://q2frame-20260930-0925",
        source_table="fixture:q2frame",
        trade_date="2026-09-30",
        expected_symbols=tuple(quotes),
    )

    assert fact.status.value == "READY"
    assert fact.input_symbol_count == 14
    assert fact.candidate_symbols == tuple(sorted(quotes))
    assert fact.candidate_membership_unknown_count == 0
    assert fact.metrics["limit_up_count"] == 10
    assert fact.metrics["limit_down_count"] == 4
    assert fact.market_universe_coverage_status == "UNKNOWN"
    assert fact.candidate_rule_status == "Q2_VALUE_DERIVED"


def test_missing_auction_values_remain_unknown_not_zero_or_non_candidate():
    row = dict(_captured_rows()[0])
    row.pop("am")
    row.pop("br")
    row.pop("ar")
    quotes = _quotes([row])

    fact = derive_q2_auction_summary(
        quotes,
        source_id="fixture://missing-auction-values",
        source_table="fixture:q2frame",
        trade_date="2026-09-30",
    )

    assert fact.status.value == "PARTIAL"
    assert fact.candidate_symbols == ()
    assert fact.candidate_membership_unknown_symbols == (row["symbol"],)
    assert fact.candidate_membership_unknown_count == 1
    assert fact.metrics["stock_count"] is None
    assert fact.metric_unknown_counts["stock_count"] == 1


def test_known_candidate_with_missing_amount_does_not_sum_missing_as_zero():
    row = dict(_captured_rows()[0])
    row.pop("am")
    quotes = _quotes([row])

    fact = derive_q2_auction_summary(
        quotes,
        source_id="fixture://missing-match-amount",
        source_table="fixture:q2frame",
        trade_date="2026-09-30",
    )

    assert row["symbol"] in fact.candidate_symbols
    assert fact.metrics["stock_count"] == 1
    assert fact.metrics["auction_amount_yuan"] is None
    assert fact.metric_unknown_counts["auction_amount_yuan"] == 1
    assert fact.status.value == "PARTIAL"


def test_q2_auction_summary_hash_is_independent_of_mapping_insertion_order():
    rows = _captured_rows()[:5]
    left = derive_q2_auction_summary(
        _quotes(rows),
        source_id="fixture://same-q2",
        source_table="fixture:q2frame",
        trade_date="2026-09-30",
    )
    right = derive_q2_auction_summary(
        _quotes(list(reversed(rows))),
        source_id="fixture://same-q2",
        source_table="fixture:q2frame",
        trade_date="2026-09-30",
    )

    assert left.candidate_symbols == right.candidate_symbols
    assert left.content_hash == right.content_hash


def test_missing_expected_q2_symbol_is_kept_as_unknown_candidate():
    rows = _captured_rows()[:1]
    quotes = _quotes(rows)
    expected = tuple(quotes) + ("600000",)

    fact = derive_q2_auction_summary(
        quotes,
        source_id="fixture://missing-expected-symbol",
        source_table="fixture:q2frame",
        trade_date="2026-09-30",
        expected_symbols=expected,
    )

    assert fact.missing_symbols == ("600000",)
    assert fact.candidate_membership_unknown_symbols == ("600000",)
    assert fact.metrics["stock_count"] is None
    assert fact.status.value == "PARTIAL"


def test_explicit_zero_auction_fields_are_not_missing_but_excluded_from_cohort():
    row = dict(_captured_rows()[0])
    row.update({"am": 0, "br": 0, "ar": 0})
    quotes = _quotes([row])

    fact = derive_q2_auction_summary(
        quotes,
        source_id="fixture://explicit-zero-auction-state",
        source_table="fixture:q2frame",
        trade_date="2026-09-30",
    )

    assert fact.status.value == "READY"
    assert fact.candidate_symbols == ()
    assert fact.candidate_membership_unknown_count == 0
    assert fact.metrics["stock_count"] == 0
    assert fact.metrics["auction_amount_yuan"] == 0
    assert fact.q2_input_coverage is None
    assert fact.market_universe_coverage_status == "UNKNOWN"


def test_invalid_auction_value_is_not_coerced_to_zero():
    row = dict(_captured_rows()[0])
    row.update({"am": "NaN", "br": 0, "ar": 0})
    quotes = _quotes([row])

    fact = derive_q2_auction_summary(
        quotes,
        source_id="fixture://invalid-auction-state",
        source_table="fixture:q2frame",
        trade_date="2026-09-30",
    )

    assert fact.status.value == "PARTIAL"
    assert fact.candidate_membership_unknown_symbols == (row["symbol"],)
    assert fact.metrics["stock_count"] is None
    assert fact.metrics["auction_amount_yuan"] is None
    assert row["symbol"] + ".am" in fact.invalid_fields


def test_missing_source_time_is_unknown_and_excluded_from_target_date_metrics():
    row = dict(_captured_rows()[0])
    row.pop("ts")
    quotes = _quotes([row])

    fact = derive_q2_auction_summary(
        quotes,
        source_id="fixture://missing-source-time",
        source_table="fixture:q2frame",
        trade_date="2026-09-30",
    )

    assert fact.source_time_unknown_symbols == (row["symbol"],)
    assert fact.candidate_membership_unknown_symbols == (row["symbol"],)
    assert fact.candidate_symbols == ()
    assert fact.metrics["stock_count"] is None
    assert fact.status.value == "PARTIAL"


def test_q2_source_time_on_another_shanghai_date_is_not_used_for_summary():
    row = dict(_captured_rows()[0])
    row["ts"] = str(int(row["ts"]) - 86_400_000)
    quotes = _quotes([row])

    fact = derive_q2_auction_summary(
        quotes,
        source_id="fixture://cross-date-source-time",
        source_table="fixture:q2frame",
        trade_date="2026-09-30",
    )

    assert fact.source_date_mismatch_symbols == (row["symbol"],)
    assert fact.candidate_membership_unknown_symbols == (row["symbol"],)
    assert fact.candidate_symbols == ()
    assert fact.metrics["stock_count"] is None
    assert fact.status.value == "PARTIAL"


def test_invalid_q2_source_time_is_reported_as_invalid_and_kept_unknown():
    row = dict(_captured_rows()[0])
    row["ts"] = "not-an-epoch"
    quotes = _quotes([row])

    fact = derive_q2_auction_summary(
        quotes,
        source_id="fixture://invalid-source-time",
        source_table="fixture:q2frame",
        trade_date="2026-09-30",
    )

    assert fact.source_time_unknown_symbols == (row["symbol"],)
    assert fact.candidate_membership_unknown_symbols == (row["symbol"],)
    assert row["symbol"] + ".ts" in fact.invalid_fields
    assert fact.metrics["stock_count"] is None
