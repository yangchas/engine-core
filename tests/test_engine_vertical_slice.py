from datetime import datetime, timedelta, timezone

from engine_core import (
    DeterministicEngine,
    EngineSignal,
    MarketStateReducer,
    ProbeStrategy,
    RedisQ2ProjectionAdapter,
    SignalKind,
    WindowManager,
    WindowSpec,
)
from engine_core.windows import local_time_ms


class FakeRedis:
    def __init__(self):
        self.active = {"q2:active:2026-09-04": {"000001", "000002"}}
        self.hashes = {
            "q2:000001": {
                "mk": "SZ",
                "px": "1000",
                "pc": "990",
                "amt": "120000",
                "vol": "100",
                "ts": str(local_time_ms("2026-09-04", "09:19:59")),
            },
            "q2:000002": {
                "mk": "SZ",
                "px": "980",
                "pc": "990",
                "amt": "90000",
                "vol": "80",
                "ts": str(local_time_ms("2026-09-04", "09:19:58")),
            },
        }

    def smembers(self, key):
        return self.active.get(key, set())

    def hgetall(self, key):
        return self.hashes.get(key, {})


def _run_once():
    trade_date = "2026-09-04"
    observed_at = datetime(
        2026, 9, 4, 9, 20, tzinfo=timezone(timedelta(hours=8))
    )
    projection = RedisQ2ProjectionAdapter(FakeRedis()).read(
        trade_date,
        observed_at,
    )
    start = local_time_ms(trade_date, "09:15:00")
    end = local_time_ms(trade_date, "09:20:00")
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("auction_trial", start, end),)),
        ProbeStrategy(),
        session_id=trade_date,
        phase="AUCTION_TRIAL",
    )
    engine.submit(
        EngineSignal(
            "market-1",
            projection.envelope.effective_time_ms,
            1,
            SignalKind.MARKET_UPDATE,
            projection,
        )
    )
    engine.submit(
        EngineSignal(
            "timer-0920",
            end,
            2,
            SignalKind.TIMER,
            {"trigger_id": "AUCTION_0920", "close_windows": ("auction_trial",)},
        )
    )
    return engine.run_until_empty()


def test_q2_vertical_slice_reaches_probe_with_window_snapshot():
    result = _run_once()
    assert result.processed_signals == 2
    assert len(result.snapshots) == 1
    assert len(result.strategy_results) == 1
    snapshot = result.snapshots[0]
    assert snapshot.trigger_id == "AUCTION_0920"
    assert snapshot.coverage == 1.0
    assert snapshot.windows["auction_trial"].observation_count == 1
    assert result.strategy_results[0].state == "OBSERVE"


def test_same_input_produces_same_semantic_result():
    left = _run_once().strategy_results[0]
    right = _run_once().strategy_results[0]
    assert left.content_hash == right.content_hash
    assert left.trace["snapshot_hash"] == right.trace["snapshot_hash"]


def test_market_cross_section_excludes_non_equity_symbols():
    redis = FakeRedis()
    redis.active["q2:active:2026-09-04"].add("399001")
    redis.hashes["q2:399001"] = {
        "mk": "SZ",
        "px": "1000",
        "pc": "990",
        "amt": "999999",
        "vol": "1",
        "ts": str(local_time_ms("2026-09-04", "09:19:59")),
    }
    observed_at = datetime(2026, 9, 4, 9, 20, tzinfo=timezone(timedelta(hours=8)))
    projection = RedisQ2ProjectionAdapter(redis).read("2026-09-04", observed_at)
    reducer = MarketStateReducer()
    reducer.apply_snapshot(projection, logical_time_ms=projection.envelope.effective_time_ms)
    snapshot = reducer.build_snapshot("AUCTION_0920")
    assert snapshot.raw_market_cross_section["observed_symbol_count"] == 2
    assert snapshot.raw_market_cross_section["excluded_non_equity_count"] == 1


def test_failed_apply_without_logical_time_does_not_mutate_revision():
    reducer = MarketStateReducer()
    redis = FakeRedis()
    for values in redis.hashes.values():
        values["ts"] = "0"
    projection = RedisQ2ProjectionAdapter(redis).read(
        "2026-09-04",
        datetime(2026, 9, 4, 9, 20, tzinfo=timezone(timedelta(hours=8))),
    )
    assert projection.envelope.effective_time_ms is None
    try:
        reducer.apply_snapshot(projection)
    except ValueError:
        pass
    else:
        raise AssertionError("missing logical time should be rejected")
    assert reducer.state.revision == 0
