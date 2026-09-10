from __future__ import annotations

from datetime import datetime, timezone

from examples.run_real_previous_day_stats import run_real_previous_day_stats


def test_real_previous_day_composition_uses_core_guard_without_network():
    # Replace the physical TD callable at the script boundary; the core
    # function still derives the date, normalizes rows, and applies its guard.
    result = run_real_previous_day_stats(
        trade_date="2026-09-04",
        previous_trade_date="2026-09-03",
        symbols=("600519",),
        observed_at=datetime(2026, 9, 4, 1, 20, tzinfo=timezone.utc),
        td_kwargs={
            "host": "unused",
            "port": 6030,
            "user": "unused",
            "password": "unused",
            "database": "market_data1",
            "config": "/etc/taos",
            "timezone_name": "Asia/Shanghai",
        },
        fetch_rows_override=lambda previous, symbols: [
            {"symbol": "600519", "close": 129.8, "amount": 1000000, "volume": 20}
        ],
    )
    assert result["read_only"] is True
    assert result["result_status"] == "UNAVAILABLE"
    assert result["actual_trade_date"] == "2026-09-03"
    assert result["available_at_ms"] is None
    assert result["rows_seen"] == 1
