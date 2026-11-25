"""Compare a TD-derived previous-day limit-up pool and optionally cache it."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core.core_cache_writer import CoreCacheWriter
from engine_core.limit_up_pool_derivation import derive_limit_up_pool_from_daily_klines
from engine_core.live_input_adapters import probe_rows_and_summary
from engine_core.live_inputs import LiveInputReader

DEFAULT_TTL_SECONDS = 3 * 86400


def _date(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    if parsed.isoformat() != value:
        raise ValueError("date must be strict YYYY-MM-DD")
    return value


def _symbol(value: str) -> str:
    if not re.fullmatch(r"\d{6}", value):
        raise ValueError("symbols must be six-digit stock codes")
    return value


def _load_eod_capture(path: Path) -> dict[str, int]:
    capture = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(capture, Mapping) or capture.get("contract_version") != "RedisQ2ReadCaptureV1":
        raise ValueError("unsupported sealed Redis capture contract")
    reads = capture.get("reads")
    if not isinstance(reads, list):
        raise ValueError("sealed Redis capture reads must be a list")
    states: dict[str, int] = {}
    for item in reads:
        if not isinstance(item, Mapping) or item.get("operation") != "hgetall":
            continue
        key = item.get("key")
        if not isinstance(key, str) or not re.fullmatch(r"q2:\d{6}", key):
            continue
        symbol = key[3:]
        value = item.get("value")
        if not isinstance(value, Mapping):
            continue
        raw_state = value.get("ls")
        if isinstance(raw_state, bool):
            continue
        try:
            state = int(raw_state)
        except (TypeError, ValueError, OverflowError):
            continue
        if isinstance(raw_state, float) and not raw_state.is_integer():
            continue
        if isinstance(raw_state, str) and str(state) != raw_state.strip():
            continue
        states[symbol] = state
    return states


def _fetch_td_klines(
    symbols: Sequence[str], *, lookback_start: str, previous_trade_date: str,
    td_connect: Callable[..., Any], td_kwargs: Mapping[str, Any],
) -> tuple[dict[str, list[Sequence[Any]]], list[str]]:
    connection = td_connect(**dict(td_kwargs))
    try:
        cursor = connection.cursor()
        klines: dict[str, list[Sequence[Any]]] = {}
        missing_table_symbols: list[str] = []
        for symbol in symbols:
            symbol = _symbol(symbol)
            table = "d_" + symbol
            sql = (
                "SELECT ts, open, high, low, close, volume, amount, turnover, pct_chg "
                f"FROM {table} WHERE ts >= '{lookback_start}' "
                f"AND ts <= '{previous_trade_date}' ORDER BY ts"
            )
            try:
                cursor.execute(sql)
                fetched = cursor.fetchall()
            except Exception:
                missing_table_symbols.append(symbol)
                continue
            klines[symbol] = list(fetched)
        return klines, missing_table_symbols
    finally:
        connection.close()


def _compare(core_rows: Sequence[Mapping[str, Any]], legacy_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    core = {str(row["symbol"]): row for row in core_rows}
    legacy = {str(row["symbol"]): row for row in legacy_rows if row.get("symbol") is not None}
    core_symbols, legacy_symbols = set(core), set(legacy)
    both_symbols = core_symbols & legacy_symbols
    return {
        "core_count": len(core),
        "legacy_count": len(legacy),
        "both": len(both_symbols),
        "core_only": [
            {"symbol": symbol, "lb_days": core[symbol].get("lb_days")}
            for symbol in sorted(core_symbols - legacy_symbols)
        ],
        "legacy_only": [
            {"symbol": symbol, "lb_days": legacy[symbol].get("lb_days")}
            for symbol in sorted(legacy_symbols - core_symbols)
        ],
        "lb_diff": [
            {"symbol": symbol, "core": core[symbol].get("lb_days"), "legacy": legacy[symbol].get("lb_days")}
            for symbol in sorted(both_symbols)
            if core[symbol].get("lb_days") != legacy[symbol].get("lb_days")
        ],
    }


def run_probe(
    *, trade_date: str, previous_trade_date: str, symbols: Sequence[str],
    lookback_start: str, td_connect: Callable[..., Any],
    redis_read_client: Any, redis_write_client_factory: Callable[[], Any] | None = None,
    td_kwargs: Mapping[str, Any] | None = None, names_by_symbol: Mapping[str, str] | None = None,
    apply: bool = False, confirm_write_core_cache: bool = False,
    now_ms: int | None = None, ttl_seconds: int = DEFAULT_TTL_SECONDS,
    close_limit_state_by_symbol: Mapping[str, int] | None = None,
    st_symbols: Sequence[str] | None = None, exclude_st: bool = True,
    st_source: str | None = None,
) -> dict[str, Any]:
    trade_date, previous_trade_date, lookback_start = map(
        _date, (trade_date, previous_trade_date, lookback_start)
    )
    if previous_trade_date >= trade_date:
        raise ValueError("previous trade date must be earlier than trade date")
    if apply and not confirm_write_core_cache:
        raise PermissionError("--apply requires --confirm-write-core-cache")
    requested_symbols = tuple(sorted({_symbol(symbol) for symbol in symbols}))
    if not requested_symbols:
        raise ValueError("a bounded universe is required")

    klines_by_symbol, missing_table_symbols = _fetch_td_klines(
        requested_symbols, lookback_start=lookback_start,
        previous_trade_date=previous_trade_date, td_connect=td_connect,
        td_kwargs=td_kwargs or {},
    )
    derivation = derive_limit_up_pool_from_daily_klines(
        klines_by_symbol, previous_trade_date, names_by_symbol=names_by_symbol or {},
        close_limit_state_by_symbol=close_limit_state_by_symbol,
        st_symbols=st_symbols, exclude_st=exclude_st, st_source=st_source,
    )
    reader = LiveInputReader(redis_read_client, now_ms=lambda: int(time.time() * 1000))
    legacy_read = reader.read(
        "yest_limit_pool", trade_date=previous_trade_date,
        previous_trade_date=previous_trade_date,
    )
    legacy_rows, legacy_summary = probe_rows_and_summary(legacy_read)
    if legacy_read.redis_type == "hash":
        decoded_by_field: list[Mapping[str, Any]] = []
        for field, raw in legacy_read.rows:
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(value, Mapping):
                decoded_by_field.append({**value, "symbol": value.get("symbol") or field})
        legacy_rows = decoded_by_field
    result: dict[str, Any] = {
        "trade_date": trade_date,
        "previous_trade_date": previous_trade_date,
        "lookback_start": lookback_start,
        "universe_count": len(requested_symbols),
        "td_rows_symbol_count": len(klines_by_symbol),
        "missing_table_symbols": missing_table_symbols,
        "core_count": len(derivation.rows),
        "seal_evidence_mode": derivation.seal_evidence_mode,
        "core_seal_verified_count": sum(row.get("seal_basis") == "Q2_LS_AT_CLOSE" for row in derivation.rows),
        "core_no_seal_evidence_count": sum(
            row.get("seal_basis") == "KLINE_CLOSE_AT_LIMIT_NO_SEAL_EVIDENCE" for row in derivation.rows
        ),
        "excluded_limit_price_without_seal": dict(derivation.excluded_counts).get("limit_price_without_seal", 0),
        "st_filter": derivation.notes.get("st_filter"),
        "st_symbol_count": derivation.notes.get("st_symbol_count", 0),
        "st_excluded_members": derivation.excluded_counts.get("st", 0),
        "legacy_count": 0,
        "both": 0,
        "core_only": [],
        "legacy_only": [],
        "lb_diff": [],
        "legacy_read": legacy_summary,
        "derivation": {
            "content_hash": derivation.content_hash,
            "excluded_counts": dict(derivation.excluded_counts),
            "notes": dict(derivation.notes),
        },
    }
    result.update(_compare(derivation.rows, legacy_rows))

    target_key = f"core:cache:yest_limit_pool:{trade_date}"
    if not derivation.rows:
        write_result: dict[str, Any] = {
            "status": "EMPTY_ROWS_PRESERVES_PREVIOUS", "key": target_key,
            "rows": 0, "ttl_seconds": ttl_seconds,
        }
    elif not apply:
        write_result = {
            "status": "DRY_RUN", "key": target_key,
            "rows": len(derivation.rows), "ttl_seconds": ttl_seconds,
        }
    else:
        if redis_write_client_factory is None:
            raise ValueError("a Redis writer factory is required for --apply")
        write_client = redis_write_client_factory()
        source_parts = ["core_td_kline"]
        if derivation.notes.get("name_fallback_symbols"):
            source_parts.append("name_fallback")
        if derivation.seal_evidence_mode == "NONE":
            source_parts.append("no_seal_evidence")
        source = ";".join(source_parts)
        writer = CoreCacheWriter(
            write_client, now_ms=now_ms if now_ms is not None else int(time.time() * 1000),
        )
        rows_by_symbol = {str(row["symbol"]): row for row in derivation.rows}
        written = writer.write_hash_dataset(
            "yest_limit_pool", trade_date, rows_by_symbol,
            source=source,
            field_units={"turnover": "yuan", "close_pct": "percent_points"},
            schema_version="CoreYestLimitPoolV1",
            ttl_seconds=ttl_seconds,
        )
        write_result = {
            "status": "WRITTEN" if written.get("written") else written.get("reason"),
            "key": written.get("key", target_key),
            "meta_key": written.get("meta_key"),
            "rows": len(derivation.rows),
            "ttl_seconds": ttl_seconds,
            "meta": written.get("meta"),
        }
        if isinstance(write_result.get("meta"), dict):
            meta = {
                **write_result["meta"],
                "st_filter": result["st_filter"],
                "st_symbol_count": result["st_symbol_count"],
            }
            write_client.set(
                write_result["meta_key"],
                json.dumps(meta, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
                ex=ttl_seconds,
            )
            write_result["meta"] = meta
        close = getattr(write_client, "close", None)
        if close:
            close()
    result["write"] = write_result
    return result


def _symbols_file(path: Path) -> tuple[str, ...]:
    return tuple(sorted({_symbol(item.strip()) for item in path.read_text(encoding="utf-8").split(",") if item.strip()}))


def _st_symbols_file(path: Path) -> tuple[str, ...]:
    return tuple(sorted({_symbol(line.strip()) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--previous-trade-date", required=True)
    parser.add_argument("--universe-symbols-file", required=True, type=Path)
    parser.add_argument("--lookback-start")
    parser.add_argument("--eod-capture", type=Path, help="sealed RedisQ2ReadCaptureV1 Q2 snapshot at close")
    parser.add_argument("--st-symbols-file", type=Path, help="one six-digit ST symbol per line")
    parser.add_argument("--include-st", action="store_true", help="include ST symbols in the derived pool")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-write-core-cache", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.apply and not args.confirm_write_core_cache:
        parser.error("--apply requires --confirm-write-core-cache")
    if args.apply and not args.include_st and args.st_symbols_file is None:
        parser.error("--apply requires --st-symbols-file unless --include-st is explicit")
    if args.output.exists():
        parser.error("--output must name a new file")
    trade_date = _date(args.trade_date)
    previous_trade_date = _date(args.previous_trade_date)
    lookback_start = _date(args.lookback_start) if args.lookback_start else (
        date.fromordinal(date.fromisoformat(previous_trade_date).toordinal() - 30).isoformat()
    )
    symbols = _symbols_file(args.universe_symbols_file)
    st_symbols = _st_symbols_file(args.st_symbols_file) if args.st_symbols_file else None
    if args.st_symbols_file is None and not args.include_st:
        print("WARNING: UNKNOWN_ST_LIST; dry-run will not exclude any symbols", file=sys.stderr)
    import taos  # type: ignore[import-not-found]
    import redis  # type: ignore[import-not-found]

    read_client = redis.Redis(
        host=os.environ.get("REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        db=int(os.environ.get("REDIS_DB", "0")),
        password=os.environ.get("REDIS_PASSWORD"),
    )
    write_factory = None
    if args.apply:
        write_factory = lambda: redis.Redis(
            host=os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            db=int(os.environ.get("REDIS_DB", "0")),
            password=os.environ.get("REDIS_PASSWORD"),
        )
    try:
        result = run_probe(
            trade_date=trade_date, previous_trade_date=previous_trade_date,
            symbols=symbols, lookback_start=lookback_start,
            td_connect=taos.connect, redis_read_client=read_client,
            redis_write_client_factory=write_factory,
            td_kwargs={
                "host": os.environ.get("TDENGINE_HOST", "127.0.0.1"),
                "port": int(os.environ.get("TDENGINE_PORT", "6030")),
                "user": os.environ.get("TDENGINE_USER", "root"),
                "password": os.environ.get("TDENGINE_PASSWORD", "taosdata"),
                "database": os.environ.get("TDENGINE_DATABASE", "market_data1"),
                "config": os.environ.get("TDENGINE_CONFIG", "/etc/taos"),
                "timezone": os.environ.get("TDENGINE_TIMEZONE", "Asia/Shanghai"),
            },
            apply=args.apply, confirm_write_core_cache=args.confirm_write_core_cache,
            close_limit_state_by_symbol=_load_eod_capture(args.eod_capture) if args.eod_capture else None,
            st_symbols=st_symbols, exclude_st=not args.include_st,
            st_source=f"file:{args.st_symbols_file}" if args.st_symbols_file else None,
        )
    finally:
        close = getattr(read_client, "close", None)
        if close:
            close()
    content = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(content)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
