"""Bounded, read-only probe of the deployed engine_next context/fact path.

This is an external migration audit tool, not a core dependency.  It imports a
specified engine_next release, injects a Redis write guard, disables known
network/write hooks, and runs the existing context builder plus the existing
auction plate fact function for a small symbol set.  No Rabbit consumer,
recovery, writer, notification, or strategy action is assembled.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


WRITE_METHODS = frozenset(
    {
        "set",
        "setnx",
        "setex",
        "psetex",
        "mset",
        "msetnx",
        "hset",
        "hmset",
        "hdel",
        "expire",
        "expireat",
        "pexpire",
        "pexpireat",
        "persist",
        "delete",
        "unlink",
        "rename",
        "renamenx",
        "copy",
        "move",
        "restore",
        "zadd",
        "zrem",
        "zremrangebyscore",
        "lpush",
        "rpush",
        "lpop",
        "rpop",
        "sadd",
        "srem",
        "incr",
        "incrby",
        "incrbyfloat",
        "decr",
        "decrby",
        "pfadd",
        "xadd",
        "xtrim",
        "flushdb",
        "flushall",
        "publish",
    }
)
READ_METHODS = frozenset(
    {
        "get",
        "hget",
        "hgetall",
        "hmget",
        "hexists",
        "smembers",
        "sismember",
        "scard",
        "exists",
        "mget",
        "keys",
        "scan",
        "scan_iter",
        "zrange",
        "zrevrange",
        "zrangebyscore",
        "zrevrangebyscore",
        "zcard",
        "lrange",
        "llen",
        "type",
        "ttl",
        "pttl",
        "strlen",
        "getrange",
        "object",
        "info",
        "ping",
        "dbsize",
        "time",
        "close",
    }
)
READ_COMMANDS = frozenset(
    {
        "GET",
        "HGET",
        "HGETALL",
        "HMGET",
        "HEXISTS",
        "SMEMBERS",
        "SISMEMBER",
        "SCARD",
        "EXISTS",
        "MGET",
        "KEYS",
        "SCAN",
        "ZRANGE",
        "ZREVRANGE",
        "ZRANGEBYSCORE",
        "ZREVRANGEBYSCORE",
        "ZCARD",
        "LRANGE",
        "LLEN",
        "TYPE",
        "TTL",
        "PTTL",
        "STRLEN",
        "GETRANGE",
        "OBJECT",
        "INFO",
        "PING",
        "DBSIZE",
        "TIME",
    }
)


class GuardPipeline:
    """Guard a redis-py pipeline, including low-level execute_command calls."""

    def __init__(self, inner: Any, parent: "GuardRedis") -> None:
        self._inner = inner
        self._parent = parent

    def __getattr__(self, name: str) -> Any:
        if name in WRITE_METHODS:
            return self._blocked(name)
        if name == "execute_command":
            return self._execute_command
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            result = attr(*args, **kwargs)
            # redis-py queues commands by returning the pipeline itself.  Keep
            # the guard wrapper in the chain so a later write cannot escape.
            return self if result is self._inner else result

        return call

    def _blocked(self, name: str):
        def blocked(*args: Any, **kwargs: Any) -> None:
            self._parent.writes.append("pipeline." + name)
            raise RuntimeError("read-only probe blocked Redis pipeline write: " + name)

        return blocked

    def _execute_command(self, command: Any, *args: Any, **kwargs: Any) -> Any:
        command_name = str(command.decode() if isinstance(command, bytes) else command).upper()
        if command_name not in READ_COMMANDS:
            self._parent.writes.append("pipeline." + command_name.lower())
            raise RuntimeError("read-only probe blocked Redis pipeline write: " + command_name)
        return self._inner.execute_command(command, *args, **kwargs)


class GuardRedis:
    """Proxy a real Redis client and fail closed on direct and pipeline writes."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.writes: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name == "pipeline":
            def pipeline(*args: Any, **kwargs: Any) -> GuardPipeline:
                return GuardPipeline(self._inner.pipeline(*args, **kwargs), self)

            return pipeline
        if name in WRITE_METHODS:
            def blocked(*args: Any, **kwargs: Any) -> None:
                self.writes.append(name)
                raise RuntimeError("read-only probe blocked Redis write: " + name)

            return blocked
        if name == "execute_command":
            def execute_command(command: Any, *args: Any, **kwargs: Any) -> Any:
                command_name = str(command.decode() if isinstance(command, bytes) else command).upper()
                if command_name not in READ_COMMANDS:
                    self.writes.append("command." + command_name.lower())
                    raise RuntimeError("read-only probe blocked Redis command: " + command_name)
                return self._inner.execute_command(command, *args, **kwargs)

            return execute_command
        if name in {"eval", "evalsha", "register_script", "transaction", "multi_exec"}:
            def blocked_special(*args: Any, **kwargs: Any) -> None:
                self.writes.append(name)
                raise RuntimeError("read-only probe blocked Redis special command: " + name)

            return blocked_special
        if name not in READ_METHODS:
            raise RuntimeError("read-only probe rejected unclassified Redis method: " + name)
        return getattr(self._inner, name)


def _strict_symbols(value: str) -> tuple[str, ...]:
    symbols = tuple(sorted({item.strip() for item in value.split(",") if item.strip()}))
    if not symbols or any(len(item) != 6 or not item.isdigit() for item in symbols):
        raise ValueError("symbols must be comma-separated six-digit codes")
    return symbols


def _parse_now(value: str, *, timezone_name: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        return parsed
    return parsed.replace(tzinfo=ZoneInfo(timezone_name))


def _phase_for_request(legacy: Mapping[str, Any], now: datetime) -> Any:
    """Delegate phase selection to the audited legacy phase authority."""

    return legacy["infer_run_phase"](now)


def _load_legacy(legacy_root: Path) -> Mapping[str, Any]:
    resolved = legacy_root.resolve()
    if not (resolved / "engine_next" / "runtime").is_dir():
        raise ValueError("legacy-root must contain engine_next/runtime")
    sys.path.insert(0, str(resolved))
    from engine_next.domain.enums import RunPhase  # type: ignore[import-not-found]
    from engine_next.runtime.intraday_context_builder import (  # type: ignore[import-not-found]
        IntradayContextBuilder,
        IntradayContextRequest,
    )
    from engine_next.runtime.intraday_data_hub import (  # type: ignore[import-not-found]
        IntradayDataHub,
    )
    from engine_next.runtime.startup_self_check import (  # type: ignore[import-not-found]
        infer_run_phase,
    )
    from engine_next.strategy_skill_layer.auction_plate_buckets import (  # type: ignore[import-not-found]
        build_auction_plate_bucket_stats,
    )
    return {
        "RunPhase": RunPhase,
        "IntradayContextBuilder": IntradayContextBuilder,
        "IntradayContextRequest": IntradayContextRequest,
        "IntradayDataHub": IntradayDataHub,
        "infer_run_phase": infer_run_phase,
        "build_auction_plate_bucket_stats": build_auction_plate_bucket_stats,
    }


def probe(
    *,
    legacy_root: Path,
    trade_date: str,
    previous_trade_date: str,
    symbols: tuple[str, ...],
    now: datetime,
) -> dict[str, Any]:
    """Run the old context and fact functions behind a read-only boundary."""

    import redis  # type: ignore[import-not-found]

    legacy = _load_legacy(legacy_root)
    inner = redis.Redis(
        host=os.environ.get("REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        db=int(os.environ.get("REDIS_DB", "0")),
        password=os.environ.get("REDIS_PASSWORD"),
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    guarded = GuardRedis(inner)
    try:
        hub = legacy["IntradayDataHub"](redis_client=guarded)
        builder = legacy["IntradayContextBuilder"](intraday_hub=hub)
        # These hooks are intentionally disabled: the probe audits the read
        # assembly path, not the legacy cache/recovery/network side effects.
        builder._write_cached_session_facts = lambda **kwargs: None
        builder._load_fallback_stock_names = lambda requested: {}
        builder._sector_flow_tracker.update_and_evaluate = lambda *args, **kwargs: {}
        # Use engine_next's own phase authority.  A diagnostic probe must not
        # label an opening-time read as POSTMARKET merely because the old
        # context request constructor accepts an explicit phase.
        phase = _phase_for_request(legacy, now)
        request = legacy["IntradayContextRequest"](
            phase=phase,
            trade_date=trade_date,
            previous_trade_date=previous_trade_date,
            symbols=symbols,
            now=now,
        )
        context = builder.build(request)
        stats = legacy["build_auction_plate_bucket_stats"](context, top_n=10)
        now_ms = int(now.timestamp() * 1000)
        latest_source_ms = int(context.latest_quote_timestamp_ms or 0)
        future_source = latest_source_ms > now_ms
        return {
            "contract_version": "EngineNextContextProbeV1",
            "trade_date": trade_date,
            "previous_trade_date": previous_trade_date,
            "symbols": symbols,
            "phase": phase.value,
            "snapshot_count": len(context.stock_snapshots),
            "quote_health": {
                "probe_now_ms": now_ms,
                "latest_quote_timestamp_ms": context.latest_quote_timestamp_ms,
                "latest_quote_age_seconds": context.latest_quote_age_seconds,
                "future_source_timestamp": future_source,
                "legacy_future_timestamp_handling": (
                    "CLAMPED_TO_ZERO_AGE"
                    if future_source and context.latest_quote_age_seconds == 0
                    else "NOT_OBSERVED"
                ),
            },
            "context_rows": [
                {
                    "symbol": row.symbol,
                    "current_pct": row.current_pct,
                    "auction_amount": row.auction_amount,
                    "plate": row.plate,
                }
                for row in context.stock_snapshots
            ],
            "legacy_fact_rows": [
                {
                    "plate": row.plate_name,
                    "auction_amount": row.auction_amount,
                    "symbol_count": row.symbol_count,
                    "leader_count": row.leader_count,
                    "expectation": row.expectation,
                }
                for row in stats
            ],
            "guard_writes": tuple(guarded.writes),
            "read_only": not guarded.writes,
            "side_effect_boundary": (
                "real Redis reads plus legacy pure fact call; known cache, network, "
                "recovery, writer, notification and effect hooks disabled"
            ),
        }
    finally:
        inner.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--previous-trade-date", required=True)
    parser.add_argument("--symbols", default="600519,000001,000002")
    parser.add_argument("--now", default="09:26:00")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    symbols = _strict_symbols(args.symbols)
    now_value = args.now
    if len(now_value) == 8:
        now_value = f"{args.trade_date}T{now_value}"
    now = _parse_now(now_value, timezone_name="Asia/Shanghai")
    result = probe(
        legacy_root=args.legacy_root,
        trade_date=args.trade_date,
        previous_trade_date=args.previous_trade_date,
        symbols=symbols,
        now=now,
    )
    result["observed_at"] = datetime.now(ZoneInfo("UTC")).isoformat()
    result["legacy_root"] = str(args.legacy_root.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0 if result["read_only"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
