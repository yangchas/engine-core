"""Bounded, read-only probe of the deployed engine_next context/fact path.

This is an external migration audit tool, not a core dependency.  It imports a
specified engine_next release, injects a Redis write guard, disables hot-rank
refresh and auction recovery, and runs the existing context builder plus the
existing auction plate fact function for a small symbol set. Auction context
is sourced only from time-eligible existing Redis top-amount projections;
TD/Wencai recovery is not attempted. No Rabbit consumer, notification, or
strategy action is assembled.
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
        "hlen",
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
        "HLEN",
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
        self._queued_count = 0
        self._recorded_hgetall: list[tuple[int, str]] = []

    def __getattr__(self, name: str) -> Any:
        if name in WRITE_METHODS:
            return self._blocked(name)
        if name == "execute_command":
            return self._execute_command
        if name == "execute":
            return self.execute
        if name not in READ_METHODS:
            raise RuntimeError("read-only probe rejected unclassified Redis pipeline method: " + name)
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            result = attr(*args, **kwargs)
            # redis-py queues commands by returning the pipeline itself.  Keep
            # the guard wrapper in the chain so a later write cannot escape.
            if result is self._inner:
                command_index = self._queued_count
                self._queued_count += 1
                if name == "hgetall" and args:
                    key = self._parent._normalize_key(args[0])
                    if key in self._parent.record_keys:
                        self._recorded_hgetall.append((command_index, key))
                return self
            return result

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
        result = self._inner.execute_command(command, *args, **kwargs)
        if result is self._inner:
            command_index = self._queued_count
            self._queued_count += 1
            if command_name == "HGETALL" and args:
                key = self._parent._normalize_key(args[0])
                if key in self._parent.record_keys:
                    self._recorded_hgetall.append((command_index, key))
            return self
        return result

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        results = self._inner.execute(*args, **kwargs)
        for command_index, key in self._recorded_hgetall:
            if command_index < len(results):
                self._parent._record_hgetall(key, results[command_index])
        self._queued_count = 0
        self._recorded_hgetall.clear()
        return results


class GuardRedis:
    """Proxy a real Redis client and fail closed on direct and pipeline writes."""

    def __init__(self, inner: Any, *, record_keys: frozenset[str] = frozenset()) -> None:
        self._inner = inner
        self.writes: list[str] = []
        self.record_keys = record_keys
        self.reads: list[dict[str, Any]] = []

    @staticmethod
    def _normalize_key(key: Any) -> str:
        return str(key.decode() if isinstance(key, bytes) else key)

    def _record_hgetall(self, key: str, value: Any) -> None:
        if key not in self.record_keys:
            return
        self.reads.append(
            {
                "operation": "hgetall",
                "key": key,
                "value": dict(value) if isinstance(value, Mapping) else {},
            }
        )

    def __getattr__(self, name: str) -> Any:
        if name == "pipeline":
            def pipeline(*args: Any, **kwargs: Any) -> GuardPipeline:
                return GuardPipeline(self._inner.pipeline(*args, **kwargs), self)

            return pipeline
        if name == "hgetall":
            def hgetall(key: Any, *args: Any, **kwargs: Any) -> Any:
                result = self._inner.hgetall(key, *args, **kwargs)
                self._record_hgetall(self._normalize_key(key), result)
                return result

            return hgetall
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


def _eligible_auction_projection_tags(now: datetime | None) -> tuple[str, ...]:
    """Return auction projection anchors observable by the supplied local time.

    The seconds component is the contract boundary; subsecond values are
    intentionally discarded (for example, 09:25:06.197 is in the 09:25:06
    bucket). Unknown observation time yields no auction projection input.
    """

    if now is None:
        return ()
    shanghai = ZoneInfo("Asia/Shanghai")
    local_now = now.replace(tzinfo=shanghai) if now.tzinfo is None else now.astimezone(shanghai)
    second_of_day = local_now.hour * 3600 + local_now.minute * 60 + local_now.second
    observable_at = (
        ("0920", 9 * 3600 + 20 * 60 + 3),
        ("0924", 9 * 3600 + 24 * 60 + 10),
        ("0925", 9 * 3600 + 25 * 60 + 6),
    )
    return tuple(tag for tag, cutoff in observable_at if second_of_day >= cutoff)


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
    from engine_next.strategy_skill_layer.relative_amount import (  # type: ignore[import-not-found]
        relative_amount_floor,
    )
    from engine_next.strategy_skill_layer.stock_behavior import (  # type: ignore[import-not-found]
        classify_opening_entry_behavior,
    )
    return {
        "RunPhase": RunPhase,
        "IntradayContextBuilder": IntradayContextBuilder,
        "IntradayContextRequest": IntradayContextRequest,
        "IntradayDataHub": IntradayDataHub,
        "infer_run_phase": infer_run_phase,
        "build_auction_plate_bucket_stats": build_auction_plate_bucket_stats,
        "relative_amount_floor": relative_amount_floor,
        "classify_opening_entry_behavior": classify_opening_entry_behavior,
    }


def _opening_behavior_rows(
    legacy: Mapping[str, Any],
    snapshots: tuple[Any, ...],
    symbols: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    """Expose the legacy opening label without promoting it to a Core rule.

    The amount floor is deliberately calculated from the full context, while
    output is bounded to the requested symbols.  Values retain the legacy
    context units (amounts are Yuan and percentage fields are ratios).
    """

    floor = float(
        legacy["relative_amount_floor"](
            snapshots,
            "amount_2m",
            top_n=160,
            fallback=20_000_000,
        )
    )
    by_symbol = {str(row.symbol): row for row in snapshots}
    classify = legacy["classify_opening_entry_behavior"]
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        row = by_symbol.get(symbol)
        if row is None:
            rows.append(
                {
                    "symbol": symbol,
                    "status": "MISSING",
                    "amount_2m_floor_yuan": floor,
                    "behavior": None,
                }
            )
            continue
        rows.append(
            {
                "symbol": symbol,
                "status": "OBSERVED",
                "open_pct_ratio": row.open_pct,
                "current_pct_ratio": row.current_pct,
                "auction_amount_yuan": row.auction_amount,
                "amount_2m_yuan": row.amount_2m,
                "speed_1m_ratio": row.speed_1m,
                "amount_2m_floor_yuan": floor,
                "behavior": classify(row, amount_2m_floor=floor),
            }
        )
    return tuple(rows)


def _configure_read_only_context_builder(builder: Any, hub: Any) -> tuple[dict[str, str], list[str]]:
    """Disable builder hooks that refresh, recover, cache, or update state.

    The deployed context builder's normal path may refresh hot-rank data and
    recover an auction anchor through TD/Wencai, including Redis writeback.
    This probe instead consumes only the latest time-eligible already-persisted
    Redis top-amount projection. Missing projection data remains missing; no
    TD/Wencai fallback is run.
    """

    blocked_external_calls: list[str] = []

    def block_external(source: str):
        def blocked(*_args: Any, **_kwargs: Any) -> None:
            blocked_external_calls.append(source)
            raise RuntimeError("read-only context probe blocked external source: " + source)

        return blocked

    # Fail closed if future engine_next changes call either active refresh path.
    hub.fetch_hot_rank = block_external("hot_rank_refresh")
    hub.recover_auction_anchor = block_external("auction_anchor_recovery")

    # Do not even attempt refresh. The source data already present in Redis is
    # still loaded by prime_runtime_state below.
    builder._ensure_hot_rank_cache = lambda *_args, **_kwargs: None

    def load_timed_topn_projection(request: Any, symbols: tuple[str, ...]) -> list[dict[str, Any]]:
        eligible_tags = _eligible_auction_projection_tags(getattr(request, "now", None))
        if not eligible_tags:
            return []
        result = hub.load_auction_snapshots(request.trade_date, tags=eligible_tags)
        projection_rows = [
            dict(row)
            for row in (getattr(result, "rows", ()) or ())
            if str(row.get("tag") or "") in eligible_tags
        ]
        tag_order = {tag: index for index, tag in enumerate(("0920", "0924", "0925"))}
        observed_tags = {str(row.get("tag") or "") for row in projection_rows}
        if not observed_tags:
            return []
        selected_tag = max(observed_tags, key=tag_order.__getitem__)
        symbol_set = {str(symbol) for symbol in symbols}
        return [
            row
            for row in projection_rows
            if str(row.get("tag") or "") == selected_tag
            and str(row.get("symbol") or "") in symbol_set
        ]

    builder._load_auction_rows = load_timed_topn_projection
    builder._write_cached_session_facts = lambda **_kwargs: None
    builder._load_fallback_stock_names = lambda _requested: {}
    builder._sector_flow_tracker.update_and_evaluate = lambda *_args, **_kwargs: {}

    profile = {
        "redis_mutations": "DENIED_BY_GUARD",
        "hot_rank_refresh": "DISABLED",
        "auction_recovery": "DISABLED",
        "auction_input": "REDIS_TOP_AMOUNT_TOP_N_ONLY",
        "auction_timing": "WHOLE_SECOND_0920_09:20:03_0924_09:24:10_0925_09:25:06",
        "td_fallback": "DISABLED",
        "wencai_fallback": "DISABLED",
        "f10_name_fallback": "DISABLED",
        "session_fact_cache_write": "DISABLED",
        "sector_flow_update": "DISABLED",
    }
    return profile, blocked_external_calls


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
    recorded_quote_keys = frozenset(
        key
        for symbol in symbols
        for key in (f"stock:quote:{symbol}", f"q2:{symbol}")
    )
    guarded = GuardRedis(inner, record_keys=recorded_quote_keys)
    try:
        hub = legacy["IntradayDataHub"](redis_client=guarded)
        builder = legacy["IntradayContextBuilder"](intraday_hub=hub)
        read_only_profile, blocked_external_calls = _configure_read_only_context_builder(builder, hub)
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
        opening_behavior_rows = _opening_behavior_rows(
            legacy,
            tuple(context.stock_snapshots),
            symbols,
        )
        now_ms = int(now.timestamp() * 1000)
        latest_source_ms = int(context.latest_quote_timestamp_ms or 0)
        future_source = latest_source_ms > now_ms
        blocked_writes = tuple(guarded.writes)
        side_effect_boundary = (
            "guarded legacy reads; blocked write attempts: "
            + ",".join(blocked_writes)
            if blocked_writes
            else "real Redis reads plus legacy pure fact call; known cache, network, "
            "recovery, writer, notification and effect hooks disabled"
        )
        return {
            "contract_version": "EngineNextContextProbeV3",
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
            "auction_projection_tags_selected": tuple(
                sorted(
                    {
                        str(row.get("tag") or "")
                        for row in context.auction_map.values()
                        if str(row.get("tag") or "")
                    }
                )
            ),
            "historical_available_at": "UNKNOWN",
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
            "opening_behavior_audit": {
                "status": "OBSERVED",
                "rule_status": "UNKNOWN",
                "rule_source": "engine_next.strategy_skill_layer.stock_behavior.classify_opening_entry_behavior",
                "amount_floor_source": "engine_next.strategy_skill_layer.relative_amount.relative_amount_floor",
                "amount_floor_scope": "full_context_snapshots",
                "rows": opening_behavior_rows,
                "notes": (
                    "Legacy labels are audit observations only; no Core strategy threshold is inferred.",
                    "speed_1m and amount_2m retain legacy context units; cross-source parity remains UNKNOWN.",
                ),
            },
            "selected_quote_reads": tuple(
                sorted(
                    guarded.reads,
                    key=lambda item: (
                        str(item["key"]),
                        str(item["value"].get("ts", "")),
                    ),
                )
            ),
            "guard_writes": blocked_writes,
            "blocked_external_calls": tuple(blocked_external_calls),
            "read_only_profile": read_only_profile,
            "read_only": not blocked_writes and not blocked_external_calls,
            "side_effect_boundary": (
                side_effect_boundary
                + "; hot-rank refresh/recovery/TD/Wencai/F10/cache writes/sector-flow update disabled; "
                + "auction rows read from existing Redis 0925 projection only"
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
