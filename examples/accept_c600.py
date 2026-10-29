#!/usr/bin/env python3
"""Live acceptance for C613: Kaipan DDE -> Core TD/Redis and Guardian ledger."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
DEFAULT_TD_DATABASE = "core_dev_c590_632551160254"
HISTORY_TRADING_DAYS = 250
MAX_WORKERS = 2
OBSOLETE_COLUMNS = ("large_net", "ddx", "ddy", "ddz")
EXPECTED_COLUMNS = ("ts", "ddje", "version", "symbol")
BSE_COVERAGE_SYMBOLS = ("920071", "920206")


def _dataset_registry() -> tuple[dict, dict]:
    registry = json.loads((ROOT / "src/engine_core/guardian/datasets.registry.json").read_text(encoding="utf-8"))
    dataset = next((item for item in registry.get("items", []) if item.get("id") == "daily_dde"), None)
    if not isinstance(dataset, dict):
        raise RuntimeError("daily_dde_registry_entry_missing")
    view = dataset.get("redis_hot_view")
    if not isinstance(view, dict) or not view.get("namespace") or int(view.get("ttl_seconds") or 0) < 1:
        raise RuntimeError("daily_dde_redis_registry_missing")
    return dataset, view


def _universe_symbols(root: Path, trade_date: str) -> list[str]:
    from engine_core.data_pipeline.storage import ArtifactCatalog

    catalog = ArtifactCatalog(root)
    manifest = catalog.locate("symbol_universe", trade_date) or catalog.locate("symbol_universe")
    if manifest is None:
        raise RuntimeError("symbol_universe_missing")
    rows = catalog.read_rows(manifest)
    active = sorted({str(row.get("symbol", "")).zfill(6) for row in rows
                     if str(row.get("trade_status", "")).strip() == "1"
                     and str(row.get("symbol", "")).strip().isdigit()})
    groups = {
        "star": [symbol for symbol in active if symbol.startswith(("688", "689"))],
        "chinext": [symbol for symbol in active if symbol.startswith(("300", "301"))],
        "beijing": [symbol for symbol in active if symbol.startswith("920")],
    }
    groups["beijing"].extend(symbol for symbol in BSE_COVERAGE_SYMBOLS
                              if symbol not in groups["beijing"])
    chosen: list[str] = []
    for name in ("star", "chinext", "beijing"):
        if len(groups[name]) < 2:
            raise RuntimeError("universe_board_coverage_missing:" + name)
        chosen.extend(groups[name][:2])
    chosen.extend(symbol for symbol in active if symbol not in chosen
                  and not symbol.startswith(("688", "689", "300", "301", "920")))
    if len(chosen) < 20:
        raise RuntimeError("universe_has_fewer_than_20_active_symbols")
    return chosen[:20]


def _table_snapshot(store, database: str) -> dict:
    rows = store._run(f"SELECT COUNT(*), SUM(ddje) FROM {database}.daily_dde_v1")
    sample = store._run(
        f"SELECT ts, symbol, ddje FROM {database}.daily_dde_v1 ORDER BY ts DESC, symbol ASC LIMIT 1")
    count, total = rows[0] if rows else (0, None)
    return {"row_count": int(count), "ddje_sum": float(total) if total is not None else None,
            "latest_sample": [str(value) if index == 0 else value for index, value in enumerate(sample[0])]
            if sample else None}


def _schema_columns(store, database: str) -> list[str]:
    return [str(row[0]).lower() for row in store._run(f"DESCRIBE {database}.daily_dde_v1")]


def _migrate_dde_schema(store, database: str) -> dict:
    if not store.database_exists():
        raise RuntimeError(database + "_database_missing")
    store.ensure_schema()
    before_schema = _schema_columns(store, database)
    before_values = _table_snapshot(store, database)
    statements = []
    for column in OBSOLETE_COLUMNS:
        if column in before_schema:
            statement = f"ALTER STABLE {database}.daily_dde_v1 DROP COLUMN {column}"
            store._run(statement)
            statements.append(statement)
    after_schema = _schema_columns(store, database)
    after_values = _table_snapshot(store, database)
    if tuple(after_schema) != EXPECTED_COLUMNS:
        raise RuntimeError(database + "_schema_mismatch:" + repr(after_schema))
    if before_values != after_values:
        raise RuntimeError(database + "_ddje_changed_during_schema_migration")
    return {"before_schema": before_schema, "after_schema": after_schema, "ddl": statements,
            "before": before_values, "after": after_values}


def _redis_readback(view_config: dict, trade_date: str, partitions: dict, ttl_expected: int) -> dict:
    import redis

    namespace = str(view_config["namespace"]).rstrip(":")
    key = namespace + ":" + trade_date
    expected = {}
    for symbol, partition in partitions.items():
        row = next((item for item in partition.rows if item["date"] == trade_date), None)
        if row is not None:
            expected[symbol] = json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    client = redis.Redis.from_url(os.environ.get("REDIS_URL") or os.environ["CORE_REDIS_URL"],
                                  decode_responses=True, socket_timeout=5)
    actual = client.hgetall(key)
    ttl = int(client.ttl(key))
    fields_verified = all(actual.get(symbol) == payload for symbol, payload in expected.items())
    if not fields_verified or ttl < ttl_expected - 10:
        raise RuntimeError("redis_core_hash_readback_failed:" + json.dumps(
            {"key": key, "expected_fields": len(expected), "actual_fields": len(actual), "ttl": ttl},
            sort_keys=True))
    return {"key": key, "type": client.type(key), "fields": len(actual), "ttl": ttl,
            "readback_verified": fields_verified, "registry_ttl_seconds": ttl_expected}


def _rehearse(event_id: str, symbol: str, database: str, trade_date: str, minutes: int,
              history_trading_days: int) -> dict:
    args_json = json.dumps({"symbols": [symbol], "skip_redis": True, "target_date": trade_date,
                            "history_trading_days": history_trading_days, "max_workers": MAX_WORKERS},
                           separators=(",", ":"))
    command = [sys.executable, str(ROOT / "examples/rehearse_event.py"), "--event", event_id,
               "--minutes", str(minutes), "--td-database", database,
               "--timeout-minutes", "10",
               "--with", "daily_kline_close_1505", "--event-args-json", args_json]
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    result_line = next((line.split("=", 1)[1] for line in completed.stdout.splitlines()
                        if line.startswith("RESULT_JSON=")), None)
    report: dict = {"exit_code": completed.returncode,
                    "stdout_tail": completed.stdout.splitlines()[-12:],
                    "stderr_tail": completed.stderr.splitlines()[-12:]}
    if result_line:
        report["result"] = json.loads(Path(result_line).read_text(encoding="utf-8"))
    if completed.returncode:
        raise RuntimeError("guardian_rehearsal_failed:" + json.dumps(report, ensure_ascii=False)[:2000])
    state = report.get("result", {}).get("final_state") or {}
    safety = report.get("result", {}).get("safety_checks") or []
    rows = report.get("result", {}).get("event_ledger_rows") or []
    alerts = [row for row in rows if isinstance(row, dict) and row.get("type") == "ALERT_OUT"]
    safety_failures = [item for item in safety if not isinstance(item, dict) or item.get("status") != "PASS"]
    if state.get("status") != "SUCCEEDED":
        raise RuntimeError("guardian_ledger_not_succeeded:" + str(state.get("status")))
    if alerts:
        raise RuntimeError("guardian_event_alerts:" + str(len(alerts)))
    if not safety or safety_failures:
        failed = [(item.get("name"), item.get("status")) for item in safety_failures if isinstance(item, dict)]
        raise RuntimeError("guardian_rehearsal_safety_check_failed:" + json.dumps(failed))
    return {"exit_code": completed.returncode, "status": state.get("status"),
            "ledger_rows": len(rows), "alerts": len(alerts), "safety_checks": safety,
            "result_json": result_line, "symbols": [symbol]}


def _sync_summary(result: dict) -> dict:
    comparison = result.get("legacy_comparison") or {}
    return ({key: result.get(key) for key in ("status", "database", "requested_symbols", "request_count",
                                               "written_symbols", "rows_written", "readback_verified",
                                               "readback_mismatches", "watermark_counts",
                                               "redis_readback_verified")}
            | {"known_missing": (result.get("known_missing") or [])[:5],
               "legacy_comparison": {key: comparison.get(key)
                                     for key in ("pairs", "equal", "different", "unmatched", "reasons")}})


def _count_rows(store) -> int:
    rows = store._run("SELECT COUNT(*) FROM core_data.daily_dde_v1")
    return int(rows[0][0]) if rows else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="DDE source date; default is the read-only legacy daily_dde waterline")
    parser.add_argument("--database", default=DEFAULT_TD_DATABASE)
    parser.add_argument("--core-database", default="core_data")
    parser.add_argument("--history-trading-days", type=int, default=HISTORY_TRADING_DAYS)
    parser.add_argument("--minutes", type=int, default=3)
    args = parser.parse_args(argv)
    result = {"card": "C613", "status": "FAIL"}
    try:
        os.environ.setdefault("TD_CONFIG", "/home/exedev/validation/operator-runs/taos-cfg")
        from engine_core.data_pipeline.daily_dde import (
            TaosDdeStore, _configure_kaipan_request_timeout, build_daily_dde, run_daily_dde_sync,
        )

        if not 1 <= args.history_trading_days <= 5000:
            raise RuntimeError("history_trading_days_out_of_range")
        dataset, redis_view = _dataset_registry()
        redis_ttl = int(redis_view["ttl_seconds"])

        legacy = TaosDdeStore(args.database)
        try:
            target_date = args.date or legacy.legacy_latest_date()
        finally:
            legacy.close()
        if not target_date:
            raise RuntimeError("legacy_daily_dde_waterline_missing")
        symbols = _universe_symbols(Path(os.environ.get("CORE_DATA_ROOT", "~/core-data")).expanduser(), target_date)
        if len(symbols) != 20:
            raise RuntimeError("acceptance_requires_exactly_20_symbols")
        board_coverage = {
            "star": sum(symbol.startswith(("688", "689")) for symbol in symbols),
            "chinext": sum(symbol.startswith(("300", "301")) for symbol in symbols),
            "beijing": sum(symbol.startswith("920") for symbol in symbols),
        }
        if any(count < 2 for count in board_coverage.values()):
            raise RuntimeError("acceptance_board_coverage_failed:" + json.dumps(board_coverage))

        import importlib
        try:
            from pykaipan import pykaipan
        except ImportError:
            pykaipan = importlib.import_module("pykaipan.pykaipan")
        _configure_kaipan_request_timeout(pykaipan)

        dev_schema_store = TaosDdeStore(args.database)
        try:
            dev_migration = _migrate_dde_schema(dev_schema_store, args.database)
        finally:
            dev_schema_store.close()

        migration_store = TaosDdeStore(args.core_database)
        try:
            migration = _migrate_dde_schema(migration_store, args.core_database)
        finally:
            migration_store.close()

        live_responses: dict[str, dict] = {}
        live_requests = 0
        request_lock = threading.Lock()

        def live_fetcher(symbol: str, day: str):
            nonlocal live_requests
            with request_lock:
                live_requests += 1
            print("FETCH_START symbol=" + symbol, flush=True)
            response = pykaipan.getHisStockDDE(symbol, day)
            live_responses[symbol] = response
            print("FETCH_DONE symbol=" + symbol + " dates=" + str(len(response.get("Date", []))), flush=True)
            return response

        dev_store = TaosDdeStore(args.database)
        original_write = dev_store.write_and_verify
        original_compare = dev_store.compare_legacy

        def logged_write(symbol, rows, version):
            print("TD_WRITE_START symbol=" + symbol + " rows=" + str(len(rows)), flush=True)
            count = original_write(symbol, rows, version)
            print("TD_WRITE_DONE symbol=" + symbol + " rows=" + str(count), flush=True)
            return count

        def logged_compare(compare_symbols, rows):
            print("LEGACY_COMPARE_START symbols=" + str(len(compare_symbols))
                  + " rows=" + str(len(rows)), flush=True)
            comparison = original_compare(compare_symbols, rows)
            print("LEGACY_COMPARE_DONE pairs=" + str(comparison.get("pairs")), flush=True)
            return comparison

        dev_store.write_and_verify = logged_write
        dev_store.compare_legacy = logged_compare
        print("CORE_DEV_SYNC_START", flush=True)
        try:
            dev = run_daily_dde_sync(
                symbols, target_date, database=args.database, store=dev_store,
                max_workers=2, attempts=1, history_trading_days=args.history_trading_days,
                redis_ttl_seconds=redis_ttl, write_redis=True, fetcher=live_fetcher,
            )
        finally:
            dev_store.close()
        print("CORE_DEV_SYNC_DONE", flush=True)
        if (dev.get("status") != "ok" or dev.get("requested_symbols") != 20
                or dev.get("request_count") != 20 or live_requests != 20
                or dev.get("readback_mismatches") or not dev.get("readback_verified")):
            raise RuntimeError("core_dev_td_readback_failed:" + json.dumps(_sync_summary(dev), ensure_ascii=False, default=str))
        if len(live_responses) != 20:
            raise RuntimeError("kaipan_unique_symbol_response_count_mismatch")

        partitions = {symbol: build_daily_dde(
            live_responses[symbol], symbol=symbol, target_date=target_date,
            history_trading_days=args.history_trading_days,
        ) for symbol in symbols}
        if any(len(partition.rows) > args.history_trading_days for partition in partitions.values()):
            raise RuntimeError("history_window_limit_failed")
        redis_result = _redis_readback(redis_view, target_date, partitions, redis_ttl)

        guardian = _rehearse("daily_dde_postclose", symbols[0], args.database, target_date,
                             args.minutes, args.history_trading_days)

        core_store = TaosDdeStore(args.core_database)
        try:
            ddl = core_store.ensure_schema()
            if tuple(_schema_columns(core_store, args.core_database)) != EXPECTED_COLUMNS:
                raise RuntimeError("core_data_schema_after_ensure_mismatch")
            before_count = _count_rows(core_store)
            core_rows = 0
            for symbol in symbols:
                partition = partitions[symbol]
                core_rows += core_store.write_and_verify(
                    symbol, partition.rows, partition.manifest["version"])
            after_first_write = _count_rows(core_store)
            repeated_rows = 0
            for symbol in symbols:
                partition = partitions[symbol]
                repeated_rows += core_store.write_and_verify(
                    symbol, partition.rows, partition.manifest["version"])
            after_idempotent_write = _count_rows(core_store)
        finally:
            core_store.close()
        if after_first_write != after_idempotent_write or core_rows != repeated_rows:
            raise RuntimeError("core_data_idempotent_write_failed")

        comparison = dev.get("legacy_comparison") or {}
        result.update({
            "status": "PASS", "trade_date": target_date, "history_trading_days": args.history_trading_days,
            "max_workers": MAX_WORKERS, "symbols": len(symbols), "board_coverage": board_coverage,
            "upstream_requests": live_requests, "core_dev": _sync_summary(dev),
            "legacy_ddje_comparison": comparison,
            "legacy_zero_columns_note": "旧库 large_net/ddx/ddy/ddz 恒为 0，不是数据；本验收只对账 ddje。",
            "schema_migration": {"core_data": migration, args.database: dev_migration},
            "redis": redis_result, "guardian": guardian,
            "core_data": {"ddl": ddl, "symbols_written": len(symbols), "rows_written": core_rows,
                          "rows_read_back_twice": repeated_rows, "row_count_before": before_count,
                          "row_count_after_first_write": after_first_write,
                          "row_count_after_idempotent_write": after_idempotent_write,
                          "idempotent": after_first_write == after_idempotent_write},
        })
    except Exception as exc:  # acceptance must report machine-readable failure without credentials
        result["error"] = type(exc).__name__ + ":" + str(exc)[:3000]
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
