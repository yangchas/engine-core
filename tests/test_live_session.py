from datetime import datetime, timedelta

import pytest

from engine_core.live_session import (build_live_slice, checkpoint_ready, publish_isolated,
                                      states_at, _checkpoint_cache_path, _write_checkpoint_cache)


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


def test_null_amount_is_reported_as_missing_and_not_zero():
    states = {
        "000001": {"symbol": "000001", "pct_change": 2.0, "match_amount_yuan": 100,
                    "price_milli": 102000, "prev_close_milli": 100000,
                    "rest_bid_shares": 0, "rest_ask_shares": 0},
        "000002": {"symbol": "000002", "pct_change": -2.0, "match_amount_yuan": None,
                    "price_milli": 98000, "prev_close_milli": 100000,
                    "rest_bid_shares": 0, "rest_ask_shares": 0},
    }
    payload = build_live_slice(states, datetime(2026, 10, 15, 9, 25))
    assert payload["amount_coverage"] == 1
    assert payload["amount_missing"] == 1
    assert payload["market_match_amount_yuan"] is None
    assert payload["amount_weighted_pct_change"] is None
    assert payload["top_stocks_by_amount"][1]["match_amount_yuan"] is None


def test_missing_price_stock_remains_in_market_amount_completeness_check():
    states = {
        "priced": {"symbol": "600001", "pct_change": 1.0, "match_amount_yuan": 500},
        "unpriced": {"symbol": "600002", "pct_change": None, "match_amount_yuan": None},
    }
    payload = build_live_slice(states, datetime(2026, 10, 15, 9, 25))
    assert payload["market_match_amount_yuan"] is None
    assert payload["amount_coverage"] == 1
    assert payload["amount_missing"] == 1
    empty = build_live_slice({}, datetime(2026, 10, 15, 9, 25))
    assert empty["market_match_amount_yuan"] is None


def test_sql_state_amount_missingness_tracks_derive_state_phase():
    from engine_core.live_session import _sql_states

    class TD:
        def cursor(self): return self
        def execute(self, _query): pass
        def fetchall(self):
            return [
                (datetime(2026, 10, 15, 9, 24, 50), 0, 10000, 10000, 10000, None, 20,
                 0, 0, 0, 0, None, 0, "600001"),
                (datetime(2026, 10, 15, 9, 25, 6), 10000, 10000, None, None, None, None,
                 None, None, None, None, 500, 0, "600002"),
                (datetime(2026, 10, 15, 9, 45, 0), 10000, 10000, None, None, None, None,
                 None, None, None, None, None, 0, "600003"),
                (datetime(2026, 10, 15, 9, 45, 0), 10000, 10000, None, None, None, None,
                 None, None, None, None, 700, 0, "600004"),
            ]

    result = _sql_states(TD(), datetime(2026, 10, 15, 9, 45))
    assert result["600001"]["match_amount_yuan"] is None
    assert result["600002"]["match_amount_yuan"] == 500
    assert result["600003"]["match_amount_yuan"] is None
    assert result["600004"]["match_amount_yuan"] == 700


def test_sql_states_propagates_null_book_price_but_keeps_post_auction_amount():
    from engine_core.live_session import _sql_states

    class TD:
        def cursor(self): return self
        def execute(self, _query): pass
        def fetchall(self):
            return [
                (datetime(2026, 10, 16, 9, 20), 0, 10000, 10000, None,
                 10, 20, 10000, 10000, 5, 7, 0, 0, "600001"),
                (datetime(2026, 10, 16, 9, 25, 6), 10500, 10000, 10400, 10600,
                 None, 4, 10300, 10700, 3, 2, 500, 10, "600002"),
            ]

    states = _sql_states(TD(), datetime(2026, 10, 16, 9, 25, 6))
    auction = states["600001"]
    assert auction["match_amount_yuan"] is None
    assert auction["rest_ask_yuan"] is None
    assert auction["price_milli"] is None
    final = states["600002"]
    assert final["match_amount_yuan"] == 500
    assert final["price_milli"] == 10500


def test_sql_states_final_print_at_0925_matches_derive_state_when_bv1_null():
    from engine_core.live_session import _sql_states

    class TD:
        def cursor(self): return self
        def execute(self, _query): pass
        def fetchall(self):
            return [(datetime(2026, 10, 16, 9, 25, 0), 10500, 10000, 10400, 10600,
                     None, 4, 10300, 10700, 3, 2, 500, 10, "600003")]

    state = _sql_states(TD(), datetime(2026, 10, 16, 9, 25, 0))["600003"]
    assert state["match_amount_yuan"] == 500
    assert state["price_milli"] == 10500


def test_missing_limit_price_is_unknown_instead_of_unsealed():
    from engine_core.live_session import _safe_at_limit, build_live_slice
    state = {"symbol": "600001", "price_milli": None, "prev_close_milli": 10000,
             "rest_bid_shares": 10, "rest_ask_shares": 0, "pct_change": None,
             "match_amount_yuan": 100}
    assert _safe_at_limit(state, "up") is None
    payload = build_live_slice({"600001": state}, datetime(2026, 10, 16, 9, 25))
    assert payload["limit_up"] is None
    assert payload["limit_state_missing"] == 1


def test_states_at_uses_injected_td(tmp_path, monkeypatch):
    monkeypatch.setenv("CORE_SLICE_ROOT", str(tmp_path))
    class TD:
        def states_at(self, cutoff): return {"x": cutoff}
    cutoff = datetime(2026, 10, 15, 9, 25)
    assert states_at(TD(), cutoff) == {"x": cutoff}


def test_states_at_reuses_past_ready_checkpoint_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("CORE_SLICE_ROOT", str(tmp_path))
    monkeypatch.setenv("CORE_STATE_CACHE", "on")
    cutoff = datetime(2020, 1, 2, 9, 25, 6)
    class TD:
        calls = 0
        def states_at(self, requested):
            self.calls += 1
            return {f"{n:06d}": {"symbol": f"{n:06d}", "pct_change": 1.0}
                    for n in range(1, 4001)}
        def latest_tick_ts(self): return cutoff
    td = TD()
    first = states_at(td, cutoff)
    second = states_at(td, cutoff)
    assert first == second
    assert td.calls == 1
    assert _checkpoint_cache_path(cutoff).is_file()


def test_states_at_does_not_read_future_checkpoint_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("CORE_SLICE_ROOT", str(tmp_path))
    cutoff = datetime.now().replace(microsecond=0) + timedelta(hours=1)
    _write_checkpoint_cache(cutoff, {"000001": {"symbol": "000001", "pct_change": 99.0}})
    class TD:
        calls = 0
        def states_at(self, requested):
            self.calls += 1
            return {"000001": {"symbol": "000001", "pct_change": 1.0}}
    td = TD()
    assert states_at(td, cutoff)["000001"]["pct_change"] == 1.0
    assert td.calls == 1


def test_daily_history_context_uses_symbol_rows_and_keeps_latest_day_semantics():
    from engine_core.live_session import daily_history_context

    rows = []
    start = datetime(2026, 1, 1)
    for offset in range(20):
        rows.append({"date": (start + timedelta(days=offset)).date().isoformat(),
                     "symbol": "600001", "close": 100.0, "open": 100.0,
                     "amount_yuan": 10.0})
        rows.append({"date": (start + timedelta(days=offset)).date().isoformat(),
                     "symbol": "600002", "close": 100.0, "open": 100.0,
                     "amount_yuan": 20.0})
    latest = (start + timedelta(days=20)).date().isoformat()
    rows.append({"date": latest, "symbol": "600001", "close": 110.0,
                 "open": 110.0, "amount_yuan": 30.0})

    result = daily_history_context(rows, "2026-02-01", {"600001": {}, "600002": {}})
    assert result["stocks"]["600001"]["streak"] == 1
    assert result["stocks"]["600001"]["position_20d"] == 1.0
    assert result["stocks"]["600001"]["prev_day_amount_yuan"] == 30.0
    assert result["stocks"]["600002"]["streak"] == 0
    assert result["stocks"]["600002"]["prev_day_amount_yuan"] is None
    assert daily_history_context(rows, "2026-02-01", {"600001": {}, "600002": {}}) is result
    changed = daily_history_context(rows, "2026-02-01", {"600001": {"pct_change": 1.0}, "600002": {}})
    assert changed is not result
    assert changed["history"]["current_median_open_pct"] == 1.0



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


def test_auction_amount_baseline_uses_prior_dates_and_midrank():
    from engine_core.live_session import auction_amount_baseline_comparison

    baseline = auction_amount_baseline_comparison(
        [
            {"date": "2026-10-01", "sum_yi": 100},
            {"date": "2026-10-02", "sum_yi": 200},
            {"date": "2026-10-03", "sum_yi": 200},
            {"date": "2026-10-15", "sum_yi": 900},
        ],
        trade_date="2026-10-15",
        current_sum_yi=200,
    )
    assert baseline == {"sample_days": 3, "median_yi": 200, "multiple": 1,
                       "percentile": pytest.approx(2 / 3 * 100),
                       "minimum_yi": 100, "maximum_yi": 200,
                       "earliest_date": "2026-10-01", "latest_date": "2026-10-03"}


def test_auction_amount_baseline_empty_and_missing_return_none(tmp_path):
    from engine_core.live_session import load_auction_amount_baseline_csv

    assert load_auction_amount_baseline_csv(None, "2026-10-15") is None
    empty = tmp_path / "empty.csv"
    empty.write_text("date,rows,sum_yi,top100_share,threshold_wan\n", encoding="utf-8")
    assert load_auction_amount_baseline_csv(empty, "2026-10-15") == []


def test_market_baseline_consumer_ignores_symbol_observations():
    from engine_core.live_session import load_auction_amount_baseline_csv

    rows = load_auction_amount_baseline_csv([
        {"row_kind": "symbol_observation", "date": "2026-10-16", "symbol": "600001",
         "auction_amount_yuan": 430_000},
        {"row_kind": "market_observation", "date": "2026-10-16", "symbol_count": 5222,
         "auction_amount_yuan": 14_010_637_874},
    ], "2026-10-19")
    assert rows == [{"date": "2026-10-16", "sum_yi": 140.10637874}]


def test_append_auction_amount_baseline_preserves_missing_and_adds_observed_value():
    from engine_core.live_session import append_auction_amount_baseline

    previous = [{"date": "2026-10-14", "sum_yi": 10.0}]
    assert append_auction_amount_baseline(previous, trade_date="2026-10-15", amount_yuan=None,
                                          available_at="2026-10-15T17:40:00+08:00") == previous
    updated = append_auction_amount_baseline(previous, trade_date="2026-10-15", amount_yuan=250_000_000,
                                             available_at="2026-10-15T17:40:00+08:00")
    assert updated[-1]["sum_yi"] == 2.5
    assert updated[-1]["source"] == "postmarket_auction_summary"
