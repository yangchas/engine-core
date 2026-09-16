"""Run one real Redis Q2 observation through the public Engine path.

This is a bounded, read-only validator.  It reuses the existing Redis Q2
adapter and turns one observation into ``MARKET_UPDATE`` plus ``TIMER`` so the
opening fact-only strategy receives an Engine-created snapshot.  It never
writes Redis/TD, consumes Rabbit, sends notifications, or makes a decision.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (  # noqa: E402
    DeterministicEngine,
    EngineSignal,
    FreshnessPolicy,
    MarketStateReducer,
    OpeningShadowStrategy,
    RedisQ2ProjectionAdapter,
    SignalKind,
    WindowManager,
    WindowSpec,
    canonical_json,
)


def run_real_opening_engine_shadow(
    *,
    client,
    trade_date: str,
    symbol: str,
    observed_at: datetime,
    stale_after_ms: int,
) -> dict:
    projection = RedisQ2ProjectionAdapter(client).read(
        trade_date,
        observed_at,
        freshness_policy=FreshnessPolicy(stale_after_ms=stale_after_ms),
    )
    now_ms = int(observed_at.timestamp() * 1000)
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("opening", now_ms, now_ms + 1),)),
        OpeningShadowStrategy(scope_id=symbol),
        session_id=trade_date,
        phase="OPENING",
    )
    engine.submit(
        EngineSignal("real-opening-market", now_ms, 1, SignalKind.MARKET_UPDATE, projection)
    )
    engine.submit(
        EngineSignal(
            "real-opening-timer",
            now_ms + 1,
            2,
            SignalKind.TIMER,
            {"trigger_id": "OPENING_0932", "close_windows": ("opening",)},
        )
    )
    result = engine.run_until_empty()
    strategy_result = result.strategy_results[-1]
    return {
        "contract_version": "RealOpeningEngineShadowV1",
        "trade_date": trade_date,
        "symbol": symbol,
        "read_only": True,
        "side_effect_boundary": "Redis SMEMBERS/HGETALL + in-memory Engine only",
        "projection_status": projection.status.value,
        "projection_coverage": projection.coverage,
        "stale_symbol_count": len(projection.stale_symbols),
        "source_time_range": {
            "oldest": projection.oldest_source_time_ms,
            "newest": projection.newest_source_time_ms,
        },
        "processed_signals": result.processed_signals,
        "strategy_result": strategy_result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--stale-after-ms", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--redis-host", default=os.environ.get("REDIS_HOST", "127.0.0.1"))
    parser.add_argument("--redis-port", type=int, default=int(os.environ.get("REDIS_PORT", "6379")))
    parser.add_argument("--redis-db", type=int, default=int(os.environ.get("REDIS_DB", "0")))
    args = parser.parse_args()
    import redis  # type: ignore[import-not-found]

    client = redis.Redis(
        host=args.redis_host,
        port=args.redis_port,
        db=args.redis_db,
        password=os.environ.get("REDIS_PASSWORD"),
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    try:
        result = run_real_opening_engine_shadow(
            client=client,
            trade_date=args.trade_date,
            symbol=args.symbol,
            observed_at=datetime.fromisoformat(args.observed_at),
            stale_after_ms=args.stale_after_ms,
        )
        with args.output.open("x", encoding="utf-8") as output:
            output.write(canonical_json(result))
        print(canonical_json(result))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
