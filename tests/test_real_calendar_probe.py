from __future__ import annotations

import pytest

from examples.run_real_calendar_probe import build_calendar_probe_result


def test_calendar_probe_separates_raw_rows_from_trading_dates():
    result = build_calendar_probe_result(
        [
            {"calendar_date": "2026-09-12", "is_trading_day": "0"},
            {"calendar_date": "2026-09-11", "is_trading_day": "1"},
            {"calendar_date": "2026-09-10", "is_trading_day": "1"},
        ],
        query_start="2026-09-10",
        query_end="2026-09-12",
        version="test-v1",
        declared_valid_from="2026-09-10",
        declared_valid_to="2026-09-11",
        observed_at_ms=1,
    )

    assert result["rows_returned"] == 3
    assert result["trading_dates_count"] == 2
    assert result["trading_dates"] == ["2026-09-10", "2026-09-11"]
    assert result["calendar_semantic_hash"]
    assert result["calendar_evidence_hash"]


def test_calendar_probe_rejects_duplicate_or_unknown_source_rows():
    common = {
        "query_start": "2026-09-10",
        "query_end": "2026-09-10",
        "version": "test-v1",
        "declared_valid_from": "2026-09-10",
        "declared_valid_to": "2026-09-10",
        "observed_at_ms": 1,
    }
    with pytest.raises(ValueError, match="duplicate"):
        build_calendar_probe_result(
            [
                {"calendar_date": "2026-09-10", "is_trading_day": "1"},
                {"calendar_date": "2026-09-10", "is_trading_day": "1"},
            ],
            **common,
        )
    with pytest.raises(ValueError, match="must be 0 or 1"):
        build_calendar_probe_result(
            [{"calendar_date": "2026-09-10", "is_trading_day": "unknown"}],
            **common,
        )
    with pytest.raises(ValueError, match="date gaps"):
        build_calendar_probe_result(
            [{"calendar_date": "2026-09-10", "is_trading_day": "1"}],
            query_start="2026-09-10",
            query_end="2026-09-12",
            version="test-v1",
            declared_valid_from="2026-09-10",
            declared_valid_to="2026-09-10",
            observed_at_ms=1,
        )
