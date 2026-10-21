from datetime import datetime, timedelta, timezone

from engine_core.guardian.reconcile_plan import reconcile_plan


def test_registry_heavy_cutoff_is_the_shared_0910_boundary():
    import json
    from engine_core.guardian.registry import REGISTRY_PATH

    rules = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    assert rules["heavy_cutoffs"]["PRE_MARKET"] == "09:10:00"
    assert rules["heavy_rules"]["pre_market_heavy_cutoff"] == "09:10:00"


def test_manual_reference_items_are_operator_actions_without_gap_noise():
    items = [
        {"id": "stock_static", "artifact": {"catalog_dataset": "stock_static"},
         "late_policy": {"mode": "manual", "trigger_at": "08:30", "depends_on": []},
         "freshness": {"partition": "latest", "max_age_days": 8}},
    ]
    plan = reconcile_plan(datetime(2026, 10, 20, 9, 0, tzinfo=TZ), {"datasets": {}},
                          registry=items, trade_date="2026-10-20", prev_trade_date="2026-10-19",
                          phase="PRE_MARKET")
    assert plan == [{"id": "stock_static", "action": "NEEDS_OPERATOR", "partition": "2026-10-20",
                     "reason": "manual_reference_import_required"}]


def test_required_empty_version_is_a_fetch_gap():
    import json
    from pathlib import Path

    core_root = Path("/home/exedev/core-data")
    catalog = json.loads((core_root / "catalog.json").read_text(encoding="utf-8"))
    day = "2026-10-20"
    empty_versions = [version for version in catalog["datasets"]["daily_indicators"]["dates"][day]
                      if version.get("row_count") == 0]
    assert empty_versions, "expected authentic 2026-10-20 zero-row daily_indicators version"
    items = [{"id": "daily_indicators", "artifact": {"catalog_dataset": "daily_indicators"},
              "late_policy": {"mode": "fetch_now", "trigger_at": "08:00:00", "depends_on": []},
              "freshness": {"partition": "D", "expected_by": "08:00"},
              "cost": {"class": "light"}, "fetch_job": ["stub"]}]
    plan = reconcile_plan(datetime(2026, 10, 20, 9, 0, tzinfo=TZ), catalog,
                          trade_date=day, prev_trade_date="2026-10-19", registry=items)
    assert plan[0]["action"] == "FETCH_NOW"

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
