from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from engine_core.auction_universe_reconciliation import (
    attribute_opening_stale_symbols,
    reconcile_auction_universe,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "facts"
    / "auction_universe_reconciliation_real_20260930.json"
)


def _anchor(symbols: list[str], *, trade_date: str = "20260930", tag: str = "0925") -> dict:
    return {"trade_date": trade_date, "auction_tag": tag, "symbols": symbols}


def _td_row(
    symbol: str,
    *,
    trade_date: str = "20260930",
    tag: str = "0925",
    match: int | None = 10,
    bid: int | None = 2,
    ask: int | None = 3,
    change: int | None = 1,
) -> dict:
    return {
        "trade_date": trade_date,
        "auction_tag": tag,
        "symbol": symbol,
        "match_amt_yuan": match,
        "rest_bid_amt_yuan": bid,
        "rest_ask_amt_yuan": ask,
        "chg_bp": change,
    }


def test_equal_nonempty_universes_are_a_nonblocking_match():
    result = reconcile_auction_universe(
        _anchor(["000001", "000002"]),
        [_td_row("000002"), _td_row("000001")],
    )

    assert result["status"] == "MATCH"
    assert result["counts"]["shared_symbol_count"] == 2
    assert result["counts"]["anchor_only_count"] == 0
    assert result["counts"]["td_only_count"] == 0
    assert result["details_valid"] is True
    assert result["diagnostic_only"] is True
    assert result["blocks_downstream"] is False


def test_set_differences_are_visible_and_zero_null_profile_is_descriptive():
    result = reconcile_auction_universe(
        _anchor(["000001", "000002"]),
        [
            _td_row("000001"),
            _td_row("000003", match=0, bid=0, ask=0, change=None),
        ],
    )

    assert result["status"] == "MISMATCH_VISIBLE"
    assert result["details_valid"] is True
    assert result["counts"]["shared_symbol_count"] == 1
    assert result["counts"]["anchor_only_count"] == 1
    assert result["anchor_only_symbols"] == ["000002"]
    assert result["td_only_symbols"] == ["000003"]
    assert result["td_only_classification_counts"] == {
        "ZERO_AMOUNT_NULL_CHANGE": 1
    }
    assert "inactive" not in json.dumps(result).lower()
    assert result["blocks_downstream"] is False


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (_td_row("000001", match=0, bid=0, ask=0, change=None), "ZERO_AMOUNT_NULL_CHANGE"),
        (_td_row("000001", match=0, bid=0, ask=0, change=0), "OTHER"),
        (_td_row("000001", match=None, bid=0, ask=0, change=None), "UNKNOWN_FIELDS"),
    ],
)
def test_null_zero_and_nonzero_td_fields_keep_distinct_profiles(row, expected):
    result = reconcile_auction_universe(_anchor([]), [row])

    assert result["td_only_classification_counts"] == {expected: 1}
    assert result["status"] == "NOT_COMPARABLE"


def test_empty_source_side_is_not_comparable_not_an_empty_match():
    both_empty = reconcile_auction_universe(_anchor([]), [])
    anchor_empty = reconcile_auction_universe(_anchor([]), [_td_row("000001")])
    td_empty = reconcile_auction_universe(_anchor(["000001"]), [])

    assert both_empty["status"] == "NOT_COMPARABLE"
    assert anchor_empty["status"] == "NOT_COMPARABLE"
    assert td_empty["status"] == "NOT_COMPARABLE"
    assert both_empty["details_valid"] is False
    assert anchor_empty["details_valid"] is False
    assert td_empty["details_valid"] is False


def test_duplicates_are_reported_and_hash_is_independent_of_row_order():
    first = _td_row("000003", match=0, bid=0, ask=0, change=None)
    duplicate = dict(first)
    other = _td_row("000004", match=10)

    forward = reconcile_auction_universe(
        _anchor([]), [first, duplicate, other]
    )
    reverse = reconcile_auction_universe(
        _anchor([]), [other, duplicate, first]
    )

    assert forward["counts"]["td_duplicate_row_count"] == 1
    assert forward["duplicate_td_symbols"] == ["000003"]
    assert forward["content_hash"] == reverse["content_hash"]
    assert forward["td_only_classification_counts"] == {
        "OTHER": 1,
        "ZERO_AMOUNT_NULL_CHANGE": 1,
    }


def test_conflicting_duplicate_profiles_are_not_arbitrarily_collapsed():
    result = reconcile_auction_universe(
        _anchor([]),
        [
            _td_row("000003", match=0, bid=0, ask=0, change=None),
            _td_row("000003", match=1, bid=0, ask=0, change=None),
        ],
    )

    assert result["counts"]["td_duplicate_row_count"] == 1
    assert result["td_only_classification_counts"] == {"DUPLICATE_CONFLICT": 1}


def test_content_hash_covers_fields_used_by_the_diagnostic():
    baseline = reconcile_auction_universe(_anchor(["000001"]), [_td_row("000001")])
    changed = reconcile_auction_universe(
        _anchor(["000001"]), [_td_row("000001", match=11)]
    )

    assert baseline["content_hash"] != changed["content_hash"]


@pytest.mark.parametrize(
    ("anchor", "td_rows"),
    [
        (_anchor(["000001"], trade_date="20260929"), [_td_row("000001")]),
        (_anchor(["000001"], tag="0924"), [_td_row("000001")]),
        (_anchor(["000001"]), [_td_row("000001", trade_date="20261001")]),
        (_anchor(["000001"]), [_td_row("000001", tag="0924")]),
    ],
)
def test_cross_date_or_cross_tag_input_is_rejected(anchor, td_rows):
    with pytest.raises(ValueError):
        reconcile_auction_universe(anchor, td_rows)


@pytest.mark.parametrize(
    ("anchor", "td_rows"),
    [
        (_anchor(["1"]), []),
        (_anchor(["000001"]), [{**_td_row("000001"), "rest_bid_amt_yuan": True}]),
        (_anchor(["000001"]), [{k: v for k, v in _td_row("000001").items() if k != "chg_bp"}]),
    ],
)
def test_bad_symbol_or_td_schema_is_rejected(anchor, td_rows):
    with pytest.raises(ValueError):
        reconcile_auction_universe(anchor, td_rows)


def test_duplicate_anchor_members_are_counted_without_rejecting_siblings():
    result = reconcile_auction_universe(
        _anchor(["000001", "000001", "000002"]),
        [_td_row("000001"), _td_row("000002")],
    )

    assert result["status"] == "MATCH"
    assert result["counts"]["anchor_symbol_count"] == 2
    assert result["counts"]["anchor_duplicate_symbol_count"] == 1


def test_detail_lists_obey_limit_while_hash_covers_full_difference():
    rows = [_td_row(f"00000{i}") for i in range(1, 4)]

    compact = reconcile_auction_universe(_anchor([]), rows, detail_limit=1)
    expanded = reconcile_auction_universe(_anchor([]), rows, detail_limit=10)

    assert compact["td_only_symbols"] == ["000001"]
    assert compact["td_only_symbols_truncated"] is True
    assert expanded["td_only_symbols"] == ["000001", "000002", "000003"]
    assert compact["content_hash"] == expanded["content_hash"]


@pytest.mark.parametrize("detail_limit", [True, 1.5, -1])
def test_detail_limit_must_be_a_nonnegative_integer(detail_limit):
    with pytest.raises(ValueError):
        reconcile_auction_universe(
            _anchor(["000001"]), [_td_row("000001")], detail_limit=detail_limit
        )


def test_hash_pinned_real_derived_fixture_preserves_observed_universe_difference():
    fixture_sha256 = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert fixture_sha256 == (
        "4a2156df1293454003c5a000f9cf4e728af9b1e2fc44b306bae5e3e80aff3ce5"
    )
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    provenance = fixture["source_artifacts"]
    assert provenance["anchor_sha256"] == (
        "75c9fee7d240dff7091d30bbd2ad9a53e78d8f0b0ca03a09876abe3a764e73c9"
    )
    assert provenance["td_sha256"] == (
        "0fed873cecdb163996263ab5ad63547843acd1d1ef95b943a510ada3a038c545"
    )

    result = reconcile_auction_universe(
        {
            "trade_date": fixture["trade_date"],
            "auction_tag": fixture["auction_tag"],
            "symbols": fixture["anchor_symbols"],
        },
        fixture["td_rows"],
    )

    assert result["status"] == "MISMATCH_VISIBLE"
    assert result["counts"] == {
        "anchor_symbol_count": 2,
        "anchor_duplicate_symbol_count": 0,
        "td_row_count": 12,
        "td_unique_symbol_count": 12,
        "td_duplicate_row_count": 0,
        "shared_symbol_count": 2,
        "anchor_only_count": 0,
        "td_only_count": 10,
    }
    assert result["td_only_classification_counts"] == {
        "ZERO_AMOUNT_NULL_CHANGE": 10
    }


def test_real_20260930_stale_opening_cohort_is_descriptively_joined_to_auction_capture():
    universe = json.loads(FIXTURE.read_text(encoding="utf-8"))
    opening_path = (
        Path(__file__).parent
        / "fixtures"
        / "facts"
        / "opening_stale_auction_universe_real_20260930.json"
    )
    opening = json.loads(opening_path.read_text(encoding="utf-8"))
    assert opening["source_artifacts"]["opening_report_sha256"] == (
        "d0f7adbe5ba46b7aca3b26535ff2ec39461f97312f4547b42f49d58974c2cfe7"
    )
    assert universe["source_artifacts"]["anchor_sha256"] == (
        "75c9fee7d240dff7091d30bbd2ad9a53e78d8f0b0ca03a09876abe3a764e73c9"
    )

    result = attribute_opening_stale_symbols(
        opening["facts_by_symbol"],
        universe["td_rows"],
        trade_date=opening["trade_date"],
        auction_tag=opening["auction_tag"],
        evaluation_time_ms=opening["evaluation_time_ms"],
        stale_after_ms=opening["stale_after_ms"],
        anchor_snapshot={
            "trade_date": universe["trade_date"],
            "auction_tag": universe["auction_tag"],
            "symbols": universe["anchor_symbols"],
        },
    )

    assert result["stale_symbol_count"] == 7
    assert result["td_row_status_counts"] == {
        "TD_ROW_ZERO_AMOUNT_NULL_CHANGE": 7
    }
    assert result["anchor_status_counts"] == {"NOT_IN_ANCHOR": 7}
    assert result["td_only_zero_amount_null_change_not_stale_count"] == 3
    assert result["diagnostic_only"] is True
    assert result["blocks_downstream"] is False
    assert result["causal_attribution"] == "NOT_ESTABLISHED"
    assert set(result["by_symbol"]) == {
        "000016",
        "300082",
        "600293",
        "601059",
        "601198",
        "603183",
        "688496",
    }


def test_stale_opening_attribution_is_order_independent_and_keeps_unknowns():
    facts = {
        "000001": {"timestamp_ms": 1000},
        "000002": {"timestamp_ms": None},
        "000003": {"timestamp_ms": 12_000},
    }
    rows = [
        _td_row("000001", match=0, bid=0, ask=0, change=None),
        _td_row("000002", match=20),
    ]
    kwargs = {
        "trade_date": "20260930",
        "auction_tag": "0925",
        "evaluation_time_ms": 10_000,
        "stale_after_ms": 1_000,
        "anchor_snapshot": _anchor(["000001"]),
    }

    forward = attribute_opening_stale_symbols(facts, rows, **kwargs)
    reverse = attribute_opening_stale_symbols(
        dict(reversed(tuple(facts.items()))), list(reversed(rows)), **kwargs
    )

    assert forward["content_hash"] == reverse["content_hash"]
    assert forward["unknown_timestamp_count"] == 1
    assert forward["future_timestamp_count"] == 1
    assert forward["stale_symbol_count"] == 1
    assert forward["by_symbol"]["000001"]["td_row_status"] == (
        "TD_ROW_ZERO_AMOUNT_NULL_CHANGE"
    )
    assert forward["by_symbol"]["000001"]["anchor_status"] == "IN_ANCHOR"


def test_stale_opening_attribution_without_anchor_does_not_infer_membership():
    result = attribute_opening_stale_symbols(
        {"000001": {"timestamp_ms": 1000}},
        [_td_row("000001")],
        trade_date="20260930",
        auction_tag="0925",
        evaluation_time_ms=10_000,
        stale_after_ms=1_000,
    )

    assert result["by_symbol"]["000001"]["anchor_status"] == "ANCHOR_NOT_PROVIDED"
    assert result["by_symbol"]["000001"]["td_row_status"] == "TD_ROW_PRESENT_OTHER"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"trade_date": "20261001"},
        {"auction_tag": "0924"},
        {"evaluation_time_ms": True},
        {"stale_after_ms": -1},
    ],
)
def test_stale_opening_attribution_rejects_inconsistent_contract_inputs(kwargs):
    params = {
        "trade_date": "20260930",
        "auction_tag": "0925",
        "evaluation_time_ms": 10_000,
        "stale_after_ms": 1_000,
    }
    params.update(kwargs)
    with pytest.raises(ValueError):
        attribute_opening_stale_symbols(
            {"000001": {"timestamp_ms": 1000}},
            [_td_row("000001", trade_date="20260930", tag="0925")],
            **params,
        )
