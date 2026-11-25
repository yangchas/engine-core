from __future__ import annotations

from examples.audit_task008_core_anchor_td_snapshot import (
    compare_core_anchor_to_td_rows,
)


def _core_report(facts: dict[str, dict]) -> dict:
    return {
        "trade_date": "2026-09-30",
        "ordered": {
            "anchor_evidence": {
                "0925": {
                    "auction_anchor_facts_by_symbol": facts,
                }
            }
        },
    }


def _td_row(symbol: str, price: int | None, *, source_time: str = "09:25:06") -> dict:
    return {
        "trade_date": "20260930",
        "auction_tag": "0925",
        "symbol": symbol,
        "px_milli": price,
        "ts": f"2026-09-30T{source_time}",
    }


def test_same_date_core_anchor_value_parity_keeps_seconds_difference_diagnostic_only():
    core = _core_report(
        {
            "000001": {
                "symbol": "000001",
                "tag": "0925",
                "status": "AVAILABLE",
                "price_milli": 11_360,
                "source_time_ms": 1790731500000,
            },
            "000002": {
                "symbol": "000002",
                "tag": "0925",
                "status": "MISSING",
                "price_milli": None,
                "source_time_ms": 1790731502000,
            },
        }
    )

    result = compare_core_anchor_to_td_rows(
        core,
        [
            _td_row("000001", 11_360, source_time="09:25:06"),
            _td_row("000002", None, source_time="09:25:06"),
        ],
        trade_date="2026-09-30",
        tag="0925",
    )

    assert result["status"] == "CANONICAL_VALUE_PARITY"
    assert result["counts"]["value_match"] == 1
    assert result["counts"]["missing_match"] == 1
    assert result["counts"]["value_mismatch"] == 0
    assert result["timing_diagnostic"]["paired_source_time_delta_count"] == 1
    assert result["timing_diagnostic"]["max_abs_source_time_delta_ms"] == 6_000
    assert result["timing_diagnostic"]["affects_status"] is False
    assert result["timing_diagnostic"]["timestamp_semantics_equivalent"] == "NOT_ESTABLISHED"
    assert result["timing_diagnostic"]["arrival_latency_inferred"] is False


def test_real_value_mismatch_is_not_hidden_by_timing_difference():
    core = _core_report(
        {
            "000001": {
                "symbol": "000001",
                "tag": "0925",
                "status": "AVAILABLE",
                "price_milli": 11_361,
                "source_time_ms": 1790731500000,
            }
        }
    )

    result = compare_core_anchor_to_td_rows(
        core,
        [_td_row("000001", 11_360, source_time="09:25:06")],
        trade_date="2026-09-30",
        tag="0925",
    )

    assert result["status"] == "VALUE_MISMATCH"
    assert result["counts"]["value_mismatch"] == 1
    assert result["timing_diagnostic"]["max_abs_source_time_delta_ms"] == 6_000


def test_invalid_td_price_is_uncomparable_not_zero_or_missing():
    core = _core_report(
        {
            "000001": {
                "symbol": "000001",
                "tag": "0925",
                "status": "MISSING",
                "price_milli": None,
                "source_time_ms": None,
            }
        }
    )

    result = compare_core_anchor_to_td_rows(
        core,
        [_td_row("000001", 0)],
        trade_date="2026-09-30",
        tag="0925",
    )

    assert result["status"] == "PARTIAL_COMPARISON"
    assert result["counts"]["uncomparable_td_price"] == 1
    assert result["counts"]["missing_match"] == 0


def test_symbol_diagnostics_keep_tag_when_only_one_source_has_the_symbol():
    result = compare_core_anchor_to_td_rows(
        _core_report(
            {
                "000001": {
                    "symbol": "000001",
                    "tag": "0925",
                    "status": "MISSING",
                    "price_milli": None,
                }
            }
        ),
        [_td_row("000002", 10_000)],
        trade_date="2026-09-30",
        tag="0925",
    )

    assert {row["tag"] for row in result["symbol_comparisons"]} == {"0925"}
    assert {row["comparison"] for row in result["symbol_comparisons"]} == {
        "CORE_ONLY_SYMBOL",
        "TD_ONLY_SYMBOL",
    }


def test_td_null_vs_core_positive_is_availability_not_value_mismatch():
    result = compare_core_anchor_to_td_rows(
        _core_report(
            {
                "000001": {
                    "symbol": "000001",
                    "tag": "0925",
                    "status": "AVAILABLE",
                    "price_milli": 10_000,
                }
            }
        ),
        [_td_row("000001", None)],
        trade_date="2026-09-30",
        tag="0925",
    )

    assert result["status"] == "VALUE_MISMATCH"
    assert result["counts"]["availability_mismatch"] == 1
    assert result["counts"]["core_available_td_missing"] == 1
    assert result["counts"]["value_mismatch"] == 0
    assert result["symbol_comparisons"][0]["comparison"] == "AVAILABILITY_MISMATCH"
