import pytest

from engine_core.market_opening_report import build_market_opening_report


def _evidence(**overrides):
    base = {
        "expected_symbol_count": 3, "observed_symbol_count": 3, "missing_symbol_count": 0,
        "coverage": 1.0, "stale_symbol_count": 0,
        "amount_2m_summary": {"amount_2m_yuan_status": "available", "amount_2m_yuan_sum": 600.0,
                              "amount_2m_yuan_present_count": 3, "amount_2m_yuan_missing_count": 0},
        "limit_state_summary": {"cohort_field_status": "available",
                                "limit_state_counts": {"up_count": 1, "down_count": 0, "normal_count": 2}},
        "cross_section_facts": {"market_breadth": {"up_count": 2, "down_count": 1, "flat_count": 0, "unknown_count": 0}},
        "facts_by_symbol": {
            "000001": {"amount_2m_yuan": 300.0, "change_pct": 5.0},
            "000002": {"amount_2m_yuan": 200.0, "change_pct": -2.0},
            "000003": {"amount_2m_yuan": 100.0, "change_pct": None},
        },
    }
    base.update(overrides)
    return base


def test_report_ranks_available_values_and_never_ranks_missing():
    report = build_market_opening_report(
        trade_date="2026-10-09", event_id="OPENING_0932", data_origin="TEST",
        opening_evidence=_evidence(), top_n=5,
    )
    assert report.status == "READY"
    text = report.text
    assert "1. 000001 | amount_2m_yuan=300" in text
    assert "涨幅 Top5（可用 2 / 3）" in text and "000003 | change_pct" not in text
    assert "1. 000002 | change_pct=-2" in text.split("跌幅")[1]


def test_partial_when_coverage_incomplete_and_deterministic():
    kwargs = dict(trade_date="2026-10-09", event_id="E", data_origin="TEST",
                  opening_evidence=_evidence(coverage=0.99, missing_symbol_count=1))
    first = build_market_opening_report(**kwargs)
    assert first.status == "PARTIAL"
    assert first.content_hash == build_market_opening_report(**kwargs).content_hash
    with pytest.raises(ValueError):
        build_market_opening_report(trade_date="20261009", event_id="E", data_origin="T", opening_evidence={})
