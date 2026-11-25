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


def test_unavailable_summary_renders_metrics_as_unavailable_not_zero():
    from types import SimpleNamespace
    from engine_core.facts import FactStatus
    from engine_core.market_auction_report import build_market_auction_report

    summary = SimpleNamespace(
        status=FactStatus.UNAVAILABLE,
        metrics={"stock_count": 0, "auction_amount_yuan": 0},
        market_universe_coverage_status="UNKNOWN",
    )
    report = build_market_auction_report(
        trade_date="2026-10-13", event_id="E", data_origin="T", market_summary=summary
    )
    assert "- stock_count: unavailable" in report.text
    assert "- auction_amount_yuan: unavailable" in report.text


def test_market_auction_report_with_theme_buckets_and_plate_health():
    theme_buckets = {
        "status": "available",
        "themes": [
            {
                "plate_code": "801007",
                "name": "芯片",
                "parent_name": "电子信息",
                "amount_yuan": 1602000000,
                "mean_change_bp": -249.0
            },
            {
                "plate_code": "801015",
                "name": "算力",
                "parent_name": "计算机",
                "amount_yuan": 1453000000,
                "mean_change_bp": -282.0
            }
        ]
    }
    plate_health = {
        "status": "OK",
        "plate_count": 1522,
        "stock_count": 5868,
        "coverage": 1.0
    }
    report = build_market_auction_report(
        trade_date="2026-11-20",
        event_id="AUCTION_0925",
        data_origin="TEST",
        theme_buckets=theme_buckets,
        plate_reference_health=plate_health
    )
    assert report.status == "READY"
    assert "## 竞价题材分桶与归因 (09:25)" in report.text
    assert "芯片 (801007)" in report.text
    assert "父板块: 电子信息" in report.text
    assert "## 板块与个股映射健康度" in report.text
    assert "plate_count: 1522" in report.text
    assert report.component_status["theme_buckets"] == "READY"
    assert report.component_status["plate_reference"] == "READY"

