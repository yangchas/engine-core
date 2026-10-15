from datetime import datetime, timedelta

import pytest

from engine_core.live_session import build_live_slice, checkpoint_ready, publish_isolated, states_at


def test_isolated_publish_checks_prefix_and_ttl():
    class Redis:
        def __init__(self): self.calls = []
        def set(self, key, value, ex=None): self.calls.append((key, value, ex))
    redis = Redis()
    publish_isolated(redis, "2026-10-15", {"ok": True}, 60)
    assert redis.calls[0][0] == "core:cache:live_tracking:2026-10-15:latest"
    assert redis.calls[0][2] == 60
    with pytest.raises(ValueError):
        publish_isolated(redis, "2026-10-15", {}, 0, key="cache:legacy")


def test_slice_marks_stale_and_checkpoint_requires_data_through_cutoff():
    now = datetime(2026, 10, 15, 9, 32)
    states = {"000001": {"symbol": "000001", "pct_change": 1.0, "match_amount_yuan": 100,
                         "price_milli": 101000, "prev_close_milli": 100000, "rest_bid_shares": 0,
                         "rest_ask_shares": 0, "ts": "09:30:00"}}
    payload = build_live_slice(states, now, previous_day_final_yuan=1000)
    assert payload["data_lag_seconds"] == 120
    assert payload["status"] == "STALE"
    class TD:
        latest = datetime(2026, 10, 15, 9, 31, 59)
        def latest_tick_ts(self): return self.latest
    td = TD()
    assert checkpoint_ready(td, datetime(2026, 10, 15, 9, 32)) is False
    td.latest = datetime(2026, 10, 15, 9, 32)
    assert checkpoint_ready(td, datetime(2026, 10, 15, 9, 32)) is True


def test_states_at_uses_injected_td():
    class TD:
        def states_at(self, cutoff): return {"x": cutoff}
    cutoff = datetime(2026, 10, 15, 9, 25)
    assert states_at(TD(), cutoff) == {"x": cutoff}



def test_market_price_volume_metrics_use_priced_rows_and_handle_nan_zero_denominator():
    states = {
        "a": {"symbol": "a", "pct_change": 2.0, "match_amount_yuan": 100, "price_milli": 100000, "prev_close_milli": 100000, "rest_bid_shares": 0, "rest_ask_shares": 0},
        "b": {"symbol": "b", "pct_change": -1.0, "match_amount_yuan": 300, "price_milli": 100000, "prev_close_milli": 100000, "rest_bid_shares": 0, "rest_ask_shares": 0},
        "c": {"symbol": "c", "pct_change": float("nan"), "match_amount_yuan": 900, "price_milli": 100000, "prev_close_milli": 100000, "rest_bid_shares": 0, "rest_ask_shares": 0},
        "d": {"symbol": "d", "pct_change": None, "match_amount_yuan": 700, "price_milli": 100000, "prev_close_milli": 100000, "rest_bid_shares": 0, "rest_ask_shares": 0},
    }
    payload = build_live_slice(states, datetime(2026, 10, 15, 9, 25), previous_day_final_yuan=200)
    assert payload["market_match_amount_yuan"] == 400
    assert payload["vs_previous_day_final"] == 2
    assert payload["amount_weighted_pct_change"] == pytest.approx(-0.25)
    assert payload["down_amount_share_pct"] == 75
    assert payload["up_share_pct"] == 50
    assert payload["median_pct_change"] == pytest.approx(0.5)
    assert payload["top100_concentration_pct"] == 100
    zero = build_live_slice({"z": {"symbol": "z", "pct_change": 1, "match_amount_yuan": 0,
                                  "price_milli": 100000, "prev_close_milli": 100000,
                                  "rest_bid_shares": 0, "rest_ask_shares": 0}},
                            datetime(2026, 10, 15, 9, 25))
    assert zero["amount_weighted_pct_change"] is None
    assert zero["down_amount_share_pct"] is None
    assert zero["top100_concentration_pct"] is None


def test_market_price_volume_state_threshold_edges():
    from engine_core.live_session import market_price_volume_status
    assert market_price_volume_status(1.25, -0.01, 24.99) == "放量下杀"
    assert market_price_volume_status(1.25, 0.01, 50) == "放量上攻"
    assert market_price_volume_status(0.85, 3, 80) == "缩量"
    assert market_price_volume_status(1.249, -1, 20) == "量价无明显一致方向"


def test_latest_tick_query_uses_last_not_max_for_tdengine():
    # TDengine rejects max() on the timestamp column ("Invalid parameter data type : max"); real-TD smoke found it.
    import datetime as _dt
    from engine_core import live_session as _ls

    class Cur:
        def __init__(self): self.sql = None
        def execute(self, sql): self.sql = sql
        def fetchall(self): return [(_dt.datetime(2026, 10, 15, 9, 45, 59),)]

    cur = Cur()
    got = _ls._latest_tick(cur, _dt.datetime(2026, 10, 15, 9, 46, 0))
    assert got == _dt.datetime(2026, 10, 15, 9, 45, 59)
    assert "last(ts)" in cur.sql and "max(" not in cur.sql
