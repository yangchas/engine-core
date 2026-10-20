from datetime import datetime, timedelta, timezone

from engine_core.guardian.reconcile_plan import reconcile_plan

TZ = timezone(timedelta(hours=8))
EMPTY = {"datasets": {name: {"dates": {}} for name in (
    "limit_up_pool", "limit_reasons", "daily_kline", "hot_plates", "auction_stock_facts", "auction_baseline",
    "symbol_universe", "plate_members", "stock_theme_assignment", "daily_indicators")}}


def _plan(hour, minute, catalog=EMPTY):
    return {row["id"]: row for row in reconcile_plan(
        datetime(2026, 10, 20, hour, minute, tzinfo=TZ), catalog, trade_date="2026-10-20", prev_trade_date="2026-10-19")}


def test_limit_pool_is_fetched_at_any_late_start():
    for hour, minute in ((9, 6), (9, 16), (9, 26), (9, 36), (13, 0)):
        assert _plan(hour, minute)["limit_up_pool"]["action"] == "FETCH_NOW"


def test_time_point_capture_is_gap_never_backfilled():
    assert _plan(9, 36)["hot_plates"]["action"] == "GAP"


def test_pending_items_are_left_alone_and_dependencies_ordered():
    early = _plan(9, 22)
    assert "auction_stock_facts" not in early and "daily_kline" not in early  # not yet overdue
    late = reconcile_plan(datetime(2026, 10, 20, 9, 36, tzinfo=TZ), EMPTY, trade_date="2026-10-20",
                          prev_trade_date="2026-10-19")
    order = [row["id"] for row in late]
    assert order.index("auction_baseline") < order.index("auction_stock_facts")
    rows = {row["id"]: row for row in late}
    assert rows["auction_stock_facts"]["action"] == "FETCH_NOW"
    assert _plan(18, 45)["daily_kline"]["action"] == "FETCH_NOW"


def test_heavy_items_deferred_in_busy_phase_and_when_loaded():
    rows = {r["id"]: r for r in reconcile_plan(datetime(2026, 10, 20, 19, 30, tzinfo=TZ), EMPTY, trade_date="2026-10-20",
                                               prev_trade_date="2026-10-19", phase="OPEN")}
    assert rows["daily_kline"]["action"] == "DEFER"
    assert rows["limit_up_pool"]["action"] == "FETCH_NOW"  # light: never deferred
    ok = {r["id"]: r for r in reconcile_plan(datetime(2026, 10, 20, 19, 30, tzinfo=TZ), EMPTY, trade_date="2026-10-20",
                                             prev_trade_date="2026-10-19", phase="POST_MARKET", load1=1.0, cpus=4)}
    assert ok["daily_kline"]["action"] == "FETCH_NOW"
    busy = {r["id"]: r for r in reconcile_plan(datetime(2026, 10, 20, 19, 30, tzinfo=TZ), EMPTY, trade_date="2026-10-20",
                                               prev_trade_date="2026-10-19", phase="POST_MARKET", load1=9.0, cpus=4)}
    assert busy["daily_kline"]["action"] == "DEFER"
