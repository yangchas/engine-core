from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from examples.run_task008_replay_opening_validation import _projection, _run_pass


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _row(symbol: str, *, price: str, source_time_ms: int) -> dict[str, str]:
    return {
        "symbol": symbol,
        "px": price,
        "pc": "10000",
        "amt": "1000000",
        "amt2m": "120000",
        "ls": "0",
        "ts": str(source_time_ms),
    }


def test_replay_projection_keeps_symbol_payload_when_input_is_shuffled():
    observed_at = datetime(2026, 9, 15, 9, 32, 10, tzinfo=SHANGHAI)
    rows = [
        _row("000001", price="11850", source_time_ms=1789369200000),
        _row("000002", price="3050", source_time_ms=1789369200000),
    ]

    ordered = _run_pass(
        rows,
        trade_date="2026-09-15",
        observed_at=observed_at,
        stale_after_ms=60_000,
        symbols=("000001", "000002"),
        shuffled=False,
    )
    reversed_rows = list(reversed(rows))
    shuffled = _run_pass(
        reversed_rows,
        trade_date="2026-09-15",
        observed_at=observed_at,
        stale_after_ms=60_000,
        symbols=("000001", "000002"),
        shuffled=False,
    )

    assert ordered["projection"]["content_hash"] == shuffled["projection"]["content_hash"]
    assert ordered["engine"]["000001"]["content_hash"] == shuffled["engine"]["000001"]["content_hash"]
    assert ordered["engine"]["000002"]["content_hash"] == shuffled["engine"]["000002"]["content_hash"]
    assert ordered["engine"]["000001"]["opening_fact"]["change_pct"] > 0
    assert ordered["engine"]["000002"]["opening_fact"]["change_pct"] < 0


def test_replay_of_previous_trade_date_stays_partial_and_never_ready():
    observed_at = datetime(2026, 9, 15, 9, 32, 10, tzinfo=SHANGHAI)
    projection = _projection(
        [_row("000001", price="11850", source_time_ms=1789369200000)],
        trade_date="2026-09-15",
        observed_at=observed_at,
        stale_after_ms=60_000,
    )

    assert projection.status.value == "PARTIAL"
    assert projection.coverage == 1.0
    assert projection.stale_symbols == ("000001",)
    assert "trade_date" in projection.quotes["000001"].field_errors
    assert "stale" in projection.quotes["000001"].field_errors

