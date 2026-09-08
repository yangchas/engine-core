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
    build_a_share_session_plan,
    build_calendar_snapshot,
)
from engine_core.windows import local_time_ms


class _FakeRedis:
    def smembers(self, key):
        return {"000001"}

    def hgetall(self, key):
        return {
            "mk": "SZ",
            "px": "1000",
            "pc": "990",
            "amt": "120000",
            "vol": "100",
            "ts": str(local_time_ms("2026-09-08", "09:24:00")),
        }


def _plan():
    calendar = build_calendar_snapshot(
        ["2026-09-08"],
        version="fixture-v1",
        declared_valid_from="2026-09-08",
        declared_valid_to="2026-09-08",
        source_guard_valid_from="2026-09-08",
        source_guard_valid_to="2026-09-08",
    )
    return build_a_share_session_plan("2026-09-08", calendar)


def _engine(*, phase="UNKNOWN", session_plan=None):
    return DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("wide", 0, 10**15),)),
        ProbeStrategy(),
        session_id="2026-09-08",
        phase=phase,
        session_plan=session_plan,
    )


def test_session_plan_is_the_only_phase_authority_when_configured():
    with pytest.raises(ValueError, match="mutually exclusive"):
        _engine(phase="AUCTION", session_plan=_plan())


def test_session_plan_derives_market_update_phase_from_signal_time():
    observed_at = datetime(
        2026, 9, 8, 9, 24, tzinfo=timezone(timedelta(hours=8))
    )
    projection = RedisQ2ProjectionAdapter(_FakeRedis()).read(
        "2026-09-08", observed_at
    )
    engine = _engine(session_plan=_plan())
    engine.submit(
        EngineSignal(
            "market-0924",
            local_time_ms("2026-09-08", "09:24:00"),
            1,
            SignalKind.MARKET_UPDATE,
            projection,
        )
    )
    engine.run_until_empty()
    assert engine._reducer.state.phase == "AUCTION"


def test_timer_snapshot_uses_trigger_phase_without_rewriting_last_market_phase():
    observed_at = datetime(
        2026, 9, 8, 9, 24, tzinfo=timezone(timedelta(hours=8))
    )
    projection = RedisQ2ProjectionAdapter(_FakeRedis()).read(
        "2026-09-08", observed_at
    )
    engine = _engine(session_plan=_plan())
    engine.submit(
        EngineSignal(
            "market-0924",
            local_time_ms("2026-09-08", "09:24:00"),
            1,
            SignalKind.MARKET_UPDATE,
            projection,
        )
    )
    engine.submit(
        EngineSignal(
            "timer-0930",
            local_time_ms("2026-09-08", "09:30:00"),
            2,
            SignalKind.TIMER,
            {"trigger_id": "OPEN_0930"},
        )
    )
    result = engine.run_until_empty()
    assert result.snapshots[-1].phase == "INTRADAY"
    assert engine._reducer.state.phase == "AUCTION"


def test_session_plan_rejects_signal_from_another_local_date():
    engine = _engine(session_plan=_plan())
    engine.submit(
        EngineSignal(
            "wrong-date",
            local_time_ms("2026-09-09", "09:20:00"),
            1,
            SignalKind.TIMER,
            {"trigger_id": "WRONG_DATE"},
        )
    )
    with pytest.raises(ValueError, match="does not match session trade_date"):
        engine.run_until_empty()
    assert engine._windows.views()["wide"].finality == "OPEN"


def test_static_phase_remains_supported_without_session_plan():
    engine = _engine(phase="LEGACY_STATIC")
    engine.submit(
        EngineSignal(
            "static",
            local_time_ms("2026-09-08", "09:30:00"),
            1,
            SignalKind.TIMER,
            {"trigger_id": "STATIC"},
        )
    )
    assert engine.run_until_empty().snapshots[-1].phase == "LEGACY_STATIC"
