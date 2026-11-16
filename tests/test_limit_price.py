import pytest

from engine_core.limit_price import limit_prices, state_at_close, touched_limit


@pytest.mark.parametrize(("symbol", "pre_close", "up", "down"), [
    ("600001", 10.03, 11.03, 9.03),
    ("688001", 10.03, 12.04, 8.02),
    ("830001", 10.03, 13.04, 7.02),
])
def test_exchange_limit_prices_round_half_up_to_cents(symbol, pre_close, up, down):
    assert limit_prices(pre_close, symbol) == (up, down)


def test_st_uses_normal_band_and_close_at_limit_is_sealed_price_proxy():
    assert limit_prices(10.03, "600001", is_st=True) == (11.03, 9.03)
    assert state_at_close(11.03, 11.03, 10.1, 10.03, "600001") == "封(价格代理)"


def test_exact_touch_and_unlimited_or_suspended_prices():
    assert touched_limit(12.04, 10.03, "688001")
    assert state_at_close(10.0, 10.0, 9.0, 10.0, "688001", unlimited=True) == "UNLIMITED"
    assert state_at_close(0, 0, 0, 10.0, "600001") == "无"
    assert state_at_close(None, None, None, 10.0, "600001") == "缺价格字段"



def test_unlimited_listing_real_static_and_core_calendar():
    import json
    from pathlib import Path
    from engine_core.limit_price import is_unlimited_listing

    fixture_dir = Path(__file__).parent / "fixtures" / "real"
    calendar_data = json.loads(Path("/home/exedev/core-data/trade_calendar/calendar.json").read_text())
    calendar = [row["d"] for row in calendar_data["days"] if row["t"] == 1]
    for symbol, free_days, sixth in (
        ("688827", ("2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-16"), "2026-11-17"),
        ("001381", ("2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09"), "2026-11-10"),
    ):
        fixture = json.loads(next(fixture_dir.glob(f"td_stock_static_2026-11-16_{symbol}_*.json")).read_text())
        listing = json.loads(fixture["rows"][0][-1])["listing_date"]
        for day in free_days:
            assert is_unlimited_listing(symbol, day, listing, calendar) is True
        assert is_unlimited_listing(symbol, sixth, listing, calendar) is False
        assert is_unlimited_listing(symbol, sixth, None, calendar) is None
