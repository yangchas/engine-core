"""Single read-only Q2 observation; not a historical cutoff reconstruction."""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (
    DeterministicEngine, EngineSignal, FreshnessPolicy, MarketStateReducer,
    ProbeStrategy, RedisQ2ProjectionAdapter, SignalKind, WindowManager, WindowSpec,
)


class ReadOnlyCapture:
    """Expose only adapter read operations and retain the exact returned input."""

    def __init__(self, client):
        self.client = client
        self.reads = []

    def smembers(self, key):
        value = sorted(self.client.smembers(key))
        self.reads.append({"operation": "smembers", "key": key, "value": value})
        return value

    def hgetall(self, key):
        value = dict(self.client.hgetall(key))
        self.reads.append({"operation": "hgetall", "key": key, "value": value})
        return value


def run_engine(projection, now):
    engine = DeterministicEngine(
        MarketStateReducer(), WindowManager((WindowSpec("observation", now, now + 1),)),
        ProbeStrategy(), session_id=projection.trade_date, phase="READ_ONLY_PROBE",
    )
    engine.submit(EngineSignal("observation", now, 1, SignalKind.MARKET_UPDATE, projection))
    engine.submit(EngineSignal("probe", now + 1, 2, SignalKind.TIMER,
                               {"trigger_id": "READ_ONLY_PROBE", "close_windows": ("observation",)}))
    result = engine.run_until_empty()
    return {"processed_signals": result.processed_signals,
            "snapshot_hashes": [item.content_hash for item in result.snapshots],
            "probe_hashes": [item.content_hash for item in result.strategy_results]}


def observe(client, trade_date, observed_at, stale_after_ms):
    capture = ReadOnlyCapture(client)
    projection = RedisQ2ProjectionAdapter(capture).read(
        trade_date, observed_at, freshness_policy=FreshnessPolicy(stale_after_ms=stale_after_ms))
    now = int(observed_at.timestamp() * 1000)
    first, second = run_engine(projection, now), run_engine(projection, now)
    return {
        "trade_date": trade_date, "observed_at": observed_at.isoformat(),
        "status": projection.status.value, "consistency": projection.consistency_status,
        "requested_count": len(projection.expected_symbols), "quote_count": len(projection.quotes),
        "missing_symbols": list(projection.missing_symbols), "stale_symbols": list(projection.stale_symbols),
        "field_errors": {s: list(q.field_errors) for s, q in projection.quotes.items() if q.field_errors},
        "row_coverage": projection.coverage, "universe_authority": "NOT_PROVEN_BY_ACTIVE_SET",
        "oldest_source_time_ms": projection.oldest_source_time_ms,
        "newest_source_time_ms": projection.newest_source_time_ms,
        "projection_hash": projection.content_hash,
        "engine_run1": first, "engine_run2": second,
        "same_observation_engine_deterministic": first == second,
        "read_operations": capture.reads,
        "limitations": ["non-atomic Redis observation", "volume unit not independently verified",
                        "not historical replay or live deployment acceptance"],
        "side_effect_proof": "only smembers/hgetall exposed; TD/claim/notification/SMTP not assembled",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--stale-after-ms", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    datetime.strptime(args.trade_date, "%Y-%m-%d")
    if args.stale_after_ms < 0:
        parser.error("stale-after-ms must be nonnegative")
    import redis  # Linux runtime dependency only; no connection during imports/tests.
    client = redis.Redis(host=os.environ.get("REDIS_HOST", "127.0.0.1"),
                         port=int(os.environ.get("REDIS_PORT", "6379")),
                         db=int(os.environ.get("REDIS_DB", "0")),
                         password=os.environ.get("REDIS_PASSWORD"),
                         decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
    try:
        result = observe(client, args.trade_date, datetime.now(timezone.utc), args.stale_after_ms)
        result["read_completed_at"] = datetime.now(timezone.utc).isoformat()
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, sort_keys=True, indent=2)
        print(json.dumps({k: v for k, v in result.items() if k != "read_operations"}, ensure_ascii=False))
    finally:
        client.close()


if __name__ == "__main__":
    main()
