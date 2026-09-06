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
from engine_core.contracts import DataResult, DataStatus, FrozenDataBundle
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


def test_same_logical_time_signals_follow_stable_sequence_order():
    trade_date = "2026-09-04"
    logical_time = local_time_ms(trade_date, "09:20:00")
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("wide", 0, 10**15),)),
        ProbeStrategy(),
        session_id=trade_date,
        phase="AUCTION",
    )
    # Submit in reverse order; the queue must use signal_seq as the stable
    # order for equal logical times, independently of submission order.
    for signal_id, signal_seq, trigger_id in (
        ("signal-3", 3, "THIRD"),
        ("signal-1", 1, "FIRST"),
        ("signal-2", 2, "SECOND"),
    ):
        engine.submit(
            EngineSignal(
                signal_id,
                logical_time,
                signal_seq,
                SignalKind.TIMER,
                {"trigger_id": trigger_id},
            )
        )
    result = engine.run_until_empty()
    assert [snapshot.trigger_id for snapshot in result.snapshots] == [
        "FIRST",
        "SECOND",
        "THIRD",
    ]


def test_data_ready_submission_order_does_not_change_results():
    source = _run_once().snapshots[0]
    bundle_a = FrozenDataBundle.empty("eval-a", source.logical_time_ms)
    bundle_b = FrozenDataBundle.empty("eval-b", source.logical_time_ms)

    def run(order):
        engine = DeterministicEngine(
            MarketStateReducer(),
            WindowManager((WindowSpec("wide", 0, 10**15),)),
            ProbeStrategy(),
        )
        engine._register_evaluation("eval-a", source, ())
        engine._register_evaluation("eval-b", source, ())
        signals = {
            "a": EngineSignal(
                "data-a", source.logical_time_ms, 1, SignalKind.DATA_READY,
                {"evaluation_id": "eval-a", "bundle": bundle_a},
            ),
            "b": EngineSignal(
                "data-b", source.logical_time_ms, 2, SignalKind.DATA_READY,
                {"evaluation_id": "eval-b", "bundle": bundle_b},
            ),
        }
        for key in order:
            engine.submit(signals[key])
        result = engine.run_until_empty()
        return tuple(
            (item.evaluation_id, item.content_hash)
            for item in result.strategy_results
        )

    assert run(("b", "a")) == run(("a", "b"))
    assert run(("b", "a"))[0][0] == "eval-a"


def test_partial_q2_reaches_snapshot_without_being_promoted_to_ready():
    redis = FakeRedis()
    del redis.hashes["q2:000002"]["px"]
    observed_at = datetime(2026, 9, 4, 9, 20, tzinfo=timezone(timedelta(hours=8)))
    projection = RedisQ2ProjectionAdapter(redis).read("2026-09-04", observed_at)
    assert projection.status.value == "PARTIAL"
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec(
            "auction_trial",
            local_time_ms("2026-09-04", "09:15:00"),
            local_time_ms("2026-09-04", "09:20:00"),
        ),)),
        ProbeStrategy(),
        session_id="2026-09-04",
        phase="AUCTION_TRIAL",
    )
    engine.submit(EngineSignal(
        "partial-market",
        projection.envelope.effective_time_ms,
        1,
        SignalKind.MARKET_UPDATE,
        projection,
    ))
    engine.submit(EngineSignal(
        "partial-timer",
        local_time_ms("2026-09-04", "09:20:00"),
        2,
        SignalKind.TIMER,
        {"trigger_id": "PARTIAL_0920", "close_windows": ("auction_trial",)},
    ))
    result = engine.run_until_empty()
    assert result.snapshots[0].completeness == "PARTIAL"
    assert result.strategy_results[0].trace["completeness"] == "PARTIAL"


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
    original_revision = reducer.state.revision
    original_logical_time = reducer.state.logical_time_ms
    engine = DeterministicEngine(reducer, WindowManager((WindowSpec("auction_trial", local_time_ms(trade_date, "09:15:00"), local_time_ms(trade_date, "09:20:00")),)), ProbeStrategy(), session_id=trade_date, phase="AUCTION_TRIAL")
    old_time = local_time_ms(trade_date, "09:19:59")
    engine.submit(EngineSignal(
        "timer-old",
        old_time,
        0,
        SignalKind.TIMER,
        {"trigger_id": "EVALUATION_ORIGIN", "data_requirements": ("previous_day_stats",)},
    ))
    engine.submit(EngineSignal("timer-newer", local_time_ms(trade_date, "09:20:00"), 1, SignalKind.TIMER, {"trigger_id": "AUCTION_0920", "close_windows": ("auction_trial",)}))
    first = engine.run_until_empty()
    assert [item.trigger_id for item in first.snapshots] == ["AUCTION_0920"]
    old_evaluation_id = next(iter(engine._pending_evaluations))
    old_bundle = FrozenDataBundle.from_results(
        old_evaluation_id,
        old_time,
        ("previous_day_stats",),
        {
            "previous_day_stats": DataResult(
                request_id="old-data",
                function_id="previous_day_stats",
                status=DataStatus.UNAVAILABLE,
                data=None,
                actual_source=None,
                requested_trade_date=trade_date,
                actual_trade_date=None,
                effective_at_ms=None,
                available_at_ms=None,
                observed_at_ms=old_time,
                schema_version=1,
                completeness=0.0,
            )
        },
    )
    engine.submit(EngineSignal(
        "data-old",
        old_time,
        2,
        SignalKind.DATA_READY,
        {
            "evaluation_id": old_evaluation_id,
            "bundle": old_bundle,
        },
    ))
    second = engine.run_until_empty()
    assert [item.trigger_id for item in second.snapshots] == ["AUCTION_0920", "EVALUATION_ORIGIN"]
    assert second.strategy_results[-1].evaluation_id == old_evaluation_id
    assert reducer.state.revision == original_revision
    assert reducer.state.logical_time_ms == original_logical_time


def test_long_drain_retains_bounded_history_and_idempotency_cache():
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("wide", 0, 10**15),)),
        ProbeStrategy(),
        result_history_limit=8,
        signal_id_cache_limit=16,
    )
    for index in range(1000):
        engine.submit(EngineSignal(
            "pulse-%04d" % index,
            index + 1,
            index,
            SignalKind.PULSE,
            {"trigger_id": "PULSE_%04d" % index},
        ))
    result = engine.run_until_empty()
    assert result.processed_signals == 1000
    assert len(result.snapshots) == 8
    assert len(result.strategy_results) == 8
    assert len(engine._submitted_signatures) == 16
    assert not engine._queue


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
