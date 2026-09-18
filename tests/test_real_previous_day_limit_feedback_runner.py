from __future__ import annotations

from datetime import datetime

import examples.run_real_previous_day_limit_feedback as runner
from engine_core import DataStatus


def test_real_feedback_runner_preserves_td_change_and_source_time(monkeypatch):
    monkeypatch.setattr(
        runner,
        "query_rows",
        lambda **_: [
            (
                datetime(2026, 9, 18, 9, 25, 6, 120000),
                1305000,
                250,
                8000000,
                100000,
                200000,
                0,
                "600519",
                "20260918",
                "0925",
            )
        ],
    )

    rows = runner._load_current_0925_rows(
        trade_date="2026-09-18",
        symbols=("600519",),
        td_kwargs={},
        timezone_name="Asia/Shanghai",
    )

    assert rows == (
        {
            "trade_date": "2026-09-18",
            "tag": "0925",
            "symbol": "600519",
            "change_pct": 2.5,
            "auction_amount_yuan": 8000000,
            "source_record_time_ms": 1789694706120,
        },
    )


def test_real_feedback_runner_rebuilds_enum_string_status():
    result = runner._rebuild_previous_result(
        {
            "result_status": "DataStatus.UNAVAILABLE",
            "data": None,
            "actual_source": "redis",
            "requested_trade_date": "2026-09-18",
            "actual_trade_date": None,
            "available_at_ms": None,
            "observed_at_ms": 1_000,
            "completeness": 0.0,
            "provenance": [],
        }
    )
    assert result.status is DataStatus.UNAVAILABLE
