from datetime import datetime, timedelta, timezone

import pytest

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
from engine_core.contracts import FrozenDataBundle
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


def test_duplicate_signal_id_is_idempotent_and_conflicting_content_is_rejected():
    trade_date = "2026-09-04"
    observed_at = datetime(2026, 9, 4, 9, 20, tzinfo=timezone(timedelta(hours=8)))
    projection = RedisQ2ProjectionAdapter(FakeRedis()).read(trade_date, observed_at)
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("auction_trial", local_time_ms(trade_date, "09:15:00"), local_time_ms(trade_date, "09:20:00")),)),
        ProbeStrategy(),
        session_id=trade_date,
        phase="AUCTION_TRIAL",
    )
    signal = EngineSignal("same-id", local_time_ms(trade_date, "09:19:59"), 1, SignalKind.MARKET_UPDATE, projection)
    engine.submit(signal)
    engine.submit(signal)
    with pytest.raises(ValueError):
        engine.submit(EngineSignal("same-id", signal.logical_time_ms, 2, SignalKind.MARKET_UPDATE, projection))
    assert engine.run_until_empty().processed_signals == 1


def test_submitted_signal_payload_is_frozen_before_queueing():
    trade_date = "2026-09-04"
    end = local_time_ms(trade_date, "09:20:00")
    timer_payload = {"trigger_id": "AUCTION_0920", "close_windows": ["auction_trial"]}
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("auction_trial", local_time_ms(trade_date, "09:15:00"), end),)),
        ProbeStrategy(),
        session_id=trade_date,
        phase="AUCTION_TRIAL",
    )
    engine.submit(EngineSignal("timer-frozen", end, 1, SignalKind.TIMER, timer_payload))
    timer_payload["trigger_id"] = "MUTATED"
    result = engine.run_until_empty()
    assert result.snapshots[0].trigger_id == "AUCTION_0920"
    assert result.snapshots[0].windows["auction_trial"].finality == "FINAL"


def test_old_market_signal_cannot_move_frontier_backwards():
    # Recreate the engine so the test can submit after the first drain.
    trade_date = "2026-09-04"
    observed_at = datetime(2026, 9, 4, 9, 20, tzinfo=timezone(timedelta(hours=8)))
    projection = RedisQ2ProjectionAdapter(FakeRedis()).read(trade_date, observed_at)
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("auction_trial", local_time_ms(trade_date, "09:15:00"), local_time_ms(trade_date, "09:20:00")),)),
        ProbeStrategy(),
        session_id=trade_date,
        phase="AUCTION_TRIAL",
    )
    engine.submit(EngineSignal("newer", local_time_ms(trade_date, "09:19:59"), 1, SignalKind.MARKET_UPDATE, projection))
    engine.submit(EngineSignal("timer", local_time_ms(trade_date, "09:20:00"), 2, SignalKind.TIMER, {"trigger_id": "AUCTION_0920", "close_windows": ("auction_trial",)}))
    first = engine.run_until_empty()
    assert len(first.snapshots) == 1
    engine.submit(EngineSignal("older", local_time_ms(trade_date, "09:19:00"), 3, SignalKind.MARKET_UPDATE, projection))
    second = engine.run_until_empty()
    assert len(second.snapshots) == 1
    assert second.snapshots[0].market_state_revision == first.snapshots[0].market_state_revision


def test_old_data_ready_completes_frozen_evaluation_without_rewinding_market_state():
    trade_date = "2026-09-04"
    observed_at = datetime(2026, 9, 4, 9, 20, tzinfo=timezone(timedelta(hours=8)))
    projection = RedisQ2ProjectionAdapter(FakeRedis()).read(trade_date, observed_at)
    reducer = MarketStateReducer()
    reducer.apply_snapshot(projection, logical_time_ms=local_time_ms(trade_date, "09:19:59"), session_id=trade_date, phase="AUCTION_TRIAL")
    original_snapshot = reducer.build_snapshot("EVALUATION_ORIGIN", logical_time_ms=local_time_ms(trade_date, "09:19:59"))
    engine = DeterministicEngine(reducer, WindowManager((WindowSpec("auction_trial", local_time_ms(trade_date, "09:15:00"), local_time_ms(trade_date, "09:20:00")),)), ProbeStrategy(), session_id=trade_date, phase="AUCTION_TRIAL")
    engine.submit(EngineSignal("timer-newer", local_time_ms(trade_date, "09:20:00"), 1, SignalKind.TIMER, {"trigger_id": "AUCTION_0920", "close_windows": ("auction_trial",)}))
    first = engine.run_until_empty()
    assert [item.trigger_id for item in first.snapshots] == ["AUCTION_0920"]
    engine.submit(EngineSignal("data-old", local_time_ms(trade_date, "09:19:59"), 2, SignalKind.DATA_READY, {"snapshot": original_snapshot, "bundle": FrozenDataBundle.empty("eval-old", local_time_ms(trade_date, "09:19:59"))}))
    second = engine.run_until_empty()
    assert [item.trigger_id for item in second.snapshots] == ["AUCTION_0920", "EVALUATION_ORIGIN"]
    assert second.strategy_results[-1].evaluation_id == "eval-old"


def test_recovery_catchup_marks_closed_window_origin():
    trade_date = "2026-09-04"
    end = local_time_ms(trade_date, "09:20:00")
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("auction_trial", local_time_ms(trade_date, "09:15:00"), end),)),
        ProbeStrategy(),
        session_id=trade_date,
        phase="AUCTION_TRIAL",
    )
    engine.submit(
        EngineSignal(
            "recovery-0920",
            end,
            1,
            SignalKind.RECOVERY_CATCHUP,
            {"trigger_id": "AUCTION_0920_RECOVERY", "close_windows": ("auction_trial",)},
        )
    )
    result = engine.run_until_empty()
    assert result.snapshots[0].windows["auction_trial"].finality == "FINAL"
    assert result.snapshots[0].windows["auction_trial"].origin == "RECOVERY_CATCHUP"
