"""Run the smallest Q2 -> Engine -> Window -> Probe vertical slice."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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
from engine_core.trace import JsonTraceSink
from engine_core.windows import local_time_ms


class FakeRedis:
    def __init__(self):
        self.active = {"q2:active:2026-09-04": {"000001", "000002"}}
        self.hashes = {
            "q2:000001": {
                "mk": "SZ",
                "name": "fixture-a",
                "px": "1000",
                "pc": "990",
                "amt": "120000",
                "vol": "100",
                "ts": str(local_time_ms("2026-09-04", "09:19:59")),
                "ph": "1",
            },
            "q2:000002": {
                "mk": "SZ",
                "name": "fixture-b",
                "px": "980",
                "pc": "990",
                "amt": "90000",
                "vol": "80",
                "ts": str(local_time_ms("2026-09-04", "09:19:58")),
                "ph": "1",
            },
        }

    def smembers(self, key):
        return self.active.get(key, set())

    def hgetall(self, key):
        return self.hashes.get(key, {})


def main() -> None:
    trade_date = "2026-09-04"
    observed_at = datetime(2026, 9, 4, 9, 20, 0, tzinfo=timezone(timedelta(hours=8)))
    adapter = RedisQ2ProjectionAdapter(FakeRedis())
    projection = adapter.read(trade_date, observed_at)

    window = WindowSpec(
        "auction_trial",
        local_time_ms(trade_date, "09:15:00"),
        local_time_ms(trade_date, "09:20:00"),
    )
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((window,)),
        ProbeStrategy(),
        session_id=trade_date,
        phase="AUCTION_TRIAL",
    )
    engine.submit(
        EngineSignal(
            signal_id="fixture-market-update-1",
            logical_time_ms=projection.envelope.effective_time_ms,
            signal_seq=1,
            signal_kind=SignalKind.MARKET_UPDATE,
            payload=projection,
        )
    )
    engine.submit(
        EngineSignal(
            signal_id="fixture-auction-0920",
            logical_time_ms=local_time_ms(trade_date, "09:20:00"),
            signal_seq=2,
            signal_kind=SignalKind.TIMER,
            payload={
                "trigger_id": "AUCTION_0920",
                "close_windows": ("auction_trial",),
            },
        )
    )
    result = engine.run_until_empty()
    sink = JsonTraceSink()
    for item in result.strategy_results:
        sink.emit(item)


if __name__ == "__main__":
    main()
