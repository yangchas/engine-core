import pytest

from engine_core.market_auction_report import build_market_auction_report
from engine_core.theme_auction_delta import build_theme_auction_delta_facts


def _rows():
    return [
        {"symbol": "000001", "tag": "0925", "previous_tag": "0924", "amount_yuan": 300.0,
         "amount_delta_yuan": 200.0, "bid_amount_delta_yuan": 30.0, "change_pct_delta": 1.5,
         "amount_ratio": 3.0, "evidence_ref": "e/000001"},
        {"symbol": "000002", "tag": "0925", "previous_tag": "0924", "amount_yuan": 50.0,
         "amount_delta_yuan": None, "bid_amount_delta_yuan": 4.0, "change_pct_delta": None,
         "amount_ratio": None, "evidence_ref": "e/000002"},
    ]


def test_report_is_deterministic_and_marks_partial_without_filling_missing():
    facts = build_theme_auction_delta_facts(
        _rows(), {"000001": (("光模块", 1.0),), "000002": (("光模块", 1.0),)}
    )
    kwargs = dict(trade_date="2026-10-09", event_id="AUCTION_0925", data_origin="TEST", theme_facts=facts)
    first = build_market_auction_report(**kwargs)
    second = build_market_auction_report(**kwargs)
    assert first.content_hash == second.content_hash
    assert first.status == "PARTIAL"
    assert "delta=unavailable" in first.text and "0.0" not in first.text.split("## 主题")[1].replace("0.0%", "")


def test_report_without_any_component_is_unavailable_and_date_is_strict():
    report = build_market_auction_report(trade_date="2026-10-09", event_id="E", data_origin="TEST")
    assert report.status == "UNAVAILABLE"
    with pytest.raises(ValueError):
        build_market_auction_report(trade_date="20261009", event_id="E", data_origin="TEST")
