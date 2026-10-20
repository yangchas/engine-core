from datetime import datetime, timedelta, timezone

from engine_core.guardian.reconcile_plan import reconcile_plan

TZ = timezone(timedelta(hours=8))
NAMES = ("limit_up_pool", "limit_reasons", "daily_kline", "daily_indicators", "daily_report_features", "ladder_daily",
         "stock_day_facts", "hot_plates", "hot_rank", "auction_stock_facts", "auction_points", "auction_panorama",
         "auction_baseline", "symbol_universe", "plate_members", "stock_theme_assignment", "plate_amount_baseline",
         "stock_static")
EMPTY = {"datasets": {name: {"dates": {}} for name in NAMES}}


def _plan(hour, minute, catalog=EMPTY, **kw):
    return {row["id"]: row for row in reconcile_plan(
        datetime(2026, 10, 20, hour, minute, tzinfo=TZ), catalog, trade_date="2026-10-20",
        prev_trade_date="2026-10-19", **kw)}


def test_before_trigger_nothing_is_fetched_after_trigger_it_is():
    assert "limit_up_pool" not in _plan(8, 49)          # trigger 08:50 still ahead
    for hour, minute in ((8, 50), (8, 56), (9, 6), (9, 16), (9, 26), (9, 36), (13, 0)):
        assert _plan(hour, minute)["limit_up_pool"]["action"] == "FETCH_NOW"
    assert _plan(9, 6)["limit_up_pool"]["lane"] == "parallel"  # light: no waiting


def test_time_point_capture_is_gap_never_backfilled():
    assert _plan(9, 36)["hot_plates"]["action"] == "GAP"
    assert "hot_plates" not in _plan(9, 10)


def test_morning_start_owes_yesterdays_daily_bars_in_premarket_only():
    rows = _plan(8, 40, phase="PRE_MARKET")
    assert rows["daily_kline"]["partition"] == "2026-10-19" and rows["daily_kline"]["action"] == "FETCH_NOW"
    assert rows["daily_kline"]["lane"] == "serial"
    assert _plan(9, 12, phase="PRE_MARKET")["daily_kline"]["action"] == "DEFER"  # past the pre-market cutoff
    for phase in ("AUCTION", "OPEN", "INTRADAY_AM", "INTRADAY_PM"):
        assert _plan(9, 36, phase=phase)["daily_kline"]["action"] == "DEFER"
    assert _plan(12, 0, phase="MIDDAY_BREAK")["daily_kline"]["action"] == "FETCH_NOW"
    assert _plan(18, 45, phase="POST_MARKET")["daily_kline"]["partition"] == "2026-10-20"


def test_dependencies_ordered_and_downstream_blocked_while_upstream_deferred():
    rows = reconcile_plan(datetime(2026, 10, 20, 9, 36, tzinfo=TZ), EMPTY, trade_date="2026-10-20",
                          prev_trade_date="2026-10-19", phase="OPEN")
    order = [r["id"] for r in rows]
    assert order.index("auction_baseline") < order.index("auction_stock_facts")
    by = {r["id"]: r for r in rows}
    assert by["daily_indicators"]["action"] == "DEFER"
    assert by["auction_stock_facts"]["action"] in {"FETCH_NOW", "DEFER"}


def test_heavy_deferred_under_load():
    busy = _plan(19, 30, phase="POST_MARKET", load1=9.0, cpus=4)
    assert busy["daily_kline"]["action"] == "DEFER"
    assert _plan(19, 30, phase="POST_MARKET", load1=1.0, cpus=4)["daily_kline"]["action"] == "FETCH_NOW"
