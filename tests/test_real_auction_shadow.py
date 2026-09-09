import importlib.util
from datetime import datetime
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location(
    "real_auction_shadow",
    Path(__file__).parents[1] / "examples" / "run_real_auction_shadow.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _rows():
    return [
        (datetime(2026, 9, 9, 9, 20, 3, 83000), 1305000, -32, 10831500, 0, 2740500, 0, "600519", "20260909", "0920"),
        (datetime(2026, 9, 9, 9, 24, 10, 110000), 1305000, -32, 21532500, 5089500, 0, 0, "600519", "20260909", "0924"),
        (datetime(2026, 9, 9, 9, 25, 6, 81000), 1305010, -32, 33408300, 130500, 130501, 0, "600519", "20260909", "0925"),
    ]


def test_real_td_rows_build_two_adjacent_shadow_segments_without_engine():
    result = MODULE.build_shadow_from_rows(
        _rows(), trade_date="2026-09-09", symbol="600519"
    )

    assert [item["coverage_status"] for item in result["segments"]] == ["READY", "READY"]
    assert result["anchors"]["0920"]["source_record_time_ms"] == 1788916803083
    shadow = result["shadow"]
    # Breadth/theme are intentionally unavailable in this first symbol-only
    # migration slice, so the fact bundle remains PARTIAL even though P/M/RB/RA
    # are present.
    assert shadow["status"] == "PARTIAL"
    assert shadow["metrics"] == {
        "price_delta_milli": 10,
        "amount_delta_yuan": 11875800,
        "rest_bid_delta_yuan": -4959000,
        "rest_ask_delta_yuan": 130501,
        "pressure_delta_yuan": -5089501,
    }
    assert shadow["changes"]["price"] == "STABLE"
    assert shadow["changes"]["amount"] == "VOLUME_EXPANDING"
    assert shadow["changes"]["order_book"] == "PRESSURE_WEAKENING"
    assert shadow["state"] == "OBSERVE"
    assert shadow["decision_status"] == "FACT_ONLY"


def test_real_td_rows_require_all_three_auction_anchors():
    with pytest.raises(ValueError, match="missing auction anchors: 0925"):
        MODULE.build_shadow_from_rows(
            _rows()[:2], trade_date="2026-09-09", symbol="600519"
        )


def test_real_td_rows_reject_mismatched_symbol_and_date():
    rows = _rows()
    with pytest.raises(ValueError, match="symbol"):
        MODULE.build_shadow_from_rows(rows, trade_date="2026-09-09", symbol="000001")
    rows[0] = (*rows[0][:8], "20260908", rows[0][9])
    with pytest.raises(ValueError, match="trade_date"):
        MODULE.build_shadow_from_rows(rows, trade_date="2026-09-09", symbol="600519")
