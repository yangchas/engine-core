from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from examples.run_morning_vertical_slice_shadow import (
    _build_projection_from_raw,
    _load_calendar,
    build_morning_shadow,
)
from engine_core import local_datetime_ms


ROOT = Path(__file__).resolve().parents[1]
CALENDAR = ROOT / "tests" / "fixtures" / "calendar" / "baostock_cn_a_share_20260907.json"


def _rows() -> list[dict[str, object]]:
    return [
        {
            "auction_tag": "0920",
            "symbol": "600519",
            "trade_date": "20260914",
            "ts": datetime(2026, 9, 14, 9, 20, 3, 146000, tzinfo=timezone.utc),
            "px_milli": None,
            "chg_bp": None,
            "match_amt_yuan": 2679600,
            "rest_bid_amt_yuan": 2807200,
            "rest_ask_amt_yuan": 0,
        },
        {
            "auction_tag": "0924",
            "symbol": "600519",
            "trade_date": "20260914",
            "ts": datetime(2026, 9, 14, 9, 24, 10, 162000, tzinfo=timezone.utc),
            "px_milli": 1276000,
            "chg_bp": 6,
            "match_amt_yuan": 7783600,
            "rest_bid_amt_yuan": 3445200,
            "rest_ask_amt_yuan": 0,
        },
        {
            "auction_tag": "0925",
            "symbol": "600519",
            "trade_date": "20260914",
            "ts": datetime(2026, 9, 14, 9, 25, 6, 156000, tzinfo=timezone.utc),
            "px_milli": None,
            "chg_bp": None,
            "match_amt_yuan": 16984233,
            "rest_bid_amt_yuan": 383103,
            "rest_ask_amt_yuan": 0,
        },
    ]


def _projection():
    return _build_projection_from_raw(
        trade_date="2026-09-14",
        observed_at=datetime(2026, 9, 14, 9, 31, 20, tzinfo=timezone.utc),
        raw_hashes={
            "600519": {
                "symbol": "600519",
                "px": "1276000",
                "pc": "1270000",
                "amt": "396959488",
                "vol": "3102",
                "ts": "1789349472000",
                "br": "383103",
                "ar": "0",
                "am": "16984233",
                "mk": "sh",
                "ph": "2",
                "ls": "0",
            },
        },
        stale_after_ms=60_000,
    )


def test_morning_shadow_keeps_reference_data_unavailable_without_blocking_facts():
    calendar = _load_calendar(CALENDAR, trade_date="2026-09-14")
    result = build_morning_shadow(
        trade_date="2026-09-14",
        symbol="600519",
        projection=_projection(),
        auction_rows=_rows(),
        calendar=calendar,
        current_time_ms=local_datetime_ms("2026-09-14", "09:33:00", timezone_name="Asia/Shanghai"),
    )
    assert result["reference_data"]["previous_day_stats"]["status"] == "UNAVAILABLE"
    assert result["reference_data"]["hot_plates"]["status"] == "UNAVAILABLE"
    assert result["auction"]["shadow"]["decision_status"] == "FACT_ONLY"
    assert result["opening_transition"]["status"] == "unavailable"
    assert [item["timer_id"] for item in result["timers"]["normal"]] == [
        "AUCTION_0926",
        "OPENING_0932",
    ]


def test_morning_shadow_is_deterministic_for_same_inputs():
    calendar = _load_calendar(CALENDAR, trade_date="2026-09-14")
    kwargs = dict(
        trade_date="2026-09-14",
        symbol="600519",
        projection=_projection(),
        auction_rows=_rows(),
        calendar=calendar,
        current_time_ms=local_datetime_ms("2026-09-14", "09:33:00", timezone_name="Asia/Shanghai"),
    )
    assert build_morning_shadow(**kwargs)["semantic_hash"] == build_morning_shadow(**kwargs)["semantic_hash"]


def test_morning_shadow_does_not_synthesize_missing_0924():
    calendar = _load_calendar(CALENDAR, trade_date="2026-09-14")
    rows = [row for row in _rows() if row["auction_tag"] != "0924"]
    result = build_morning_shadow(
        trade_date="2026-09-14",
        symbol="600519",
        projection=_projection(),
        auction_rows=rows,
        calendar=calendar,
        current_time_ms=local_datetime_ms("2026-09-14", "09:33:00", timezone_name="Asia/Shanghai"),
    )
    assert result["auction"]["status"] == "UNAVAILABLE"
    assert result["auction"]["available_tags"] == ("0920", "0925")
