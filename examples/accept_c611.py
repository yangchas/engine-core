#!/usr/bin/env python3
"""Live acceptance for C611: Kaipan -> TD + Redis, followed by an isolated Guardian rehearsal."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_DATE = "2026-10-28"
DEFAULT_DATABASE = "core_dev_c590_632551160254"


def _same_value(expected: Any, actual: Any) -> bool:
    if expected is None or actual is None:
        return expected is None and actual is None
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return abs(Decimal(str(expected)) - Decimal(str(actual))) <= Decimal("0.000001")
        except (InvalidOperation, ValueError):
            return False
    return str(expected) == str(actual)


def _registry_ttl() -> int:
    path = ROOT / "src/engine_core/guardian/datasets.registry.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    item = next((row for row in payload.get("items", []) if row.get("id") == "limit_up_detail"), None)
    if not item or int(item.get("redis_ttl_seconds", 0)) <= 0:
        raise RuntimeError("registry_ttl_missing")
    return int(item["redis_ttl_seconds"])


def _check_roundtrip(days: list[str], expected_by_day: dict[str, dict[str, dict[str, Any]]], store: Any,
                     columns: tuple[str, ...]) -> tuple[bool, dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    row_counts: dict[str, dict[str, int]] = {}
    for day in days:
        expected = expected_by_day.get(day, {})
        actual = store.read_day_rows(day)
        row_counts[day] = {"expected": len(expected), "readback": len(actual)}
        if set(expected) != set(actual):
            mismatches.append({"trade_date": day, "reason": "symbol_set_mismatch",
                               "expected": len(expected), "readback": len(actual)})
        for symbol in sorted(set(expected) & set(actual)):
            for column in columns:
                if not _same_value(expected[symbol].get(column), actual[symbol].get(column)):
                    mismatches.append({"trade_date": day, "symbol": symbol, "column": column,
                                       "reason": "column_mismatch"})
                    if len(mismatches) >= 50:
                        break
            if len(mismatches) >= 50:
                break
    return not mismatches, {"row_counts": row_counts, "mismatches": mismatches,
                            "mismatch_count": len(mismatches)}


def _rehearse(database: str) -> dict[str, Any]:
    command = [sys.executable, str(ROOT / "examples/rehearse_event.py"),
               "--event", "limit_up_detail_d1", "--with", "limit_pool_d1", "--minutes", "1",
               "--refresh-output-event", "limit_pool_d1",
               "--td-database", database, "--timeout-minutes", "45"]
    completed = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=46 * 60, check=False)
    lines = completed.stdout.splitlines()
    result_path = next((line.split("=", 1)[1] for line in lines if line.startswith("RESULT_JSON=")), None)
    result: dict[str, Any] = {}
    if result_path:
        try:
            result = json.loads(Path(result_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            result = {}
    state = result.get("final_state") or {}
    status = state.get("status")
    ledger_rows = result.get("event_ledger_rows") or []
    alert_rows = [row for row in ledger_rows if isinstance(row, dict) and row.get("type") == "ALERT_OUT"]
    stderr = result.get("stderr_tail") or []
    alert_lines = [str(line) for line in stderr if "alert" in str(line).lower()]
    return {"return_code": completed.returncode, "status": status,
            "ledger_rows": len(ledger_rows) if isinstance(ledger_rows, list) else None,
            "alerts": len(alert_rows),
            "alert_lines": len(alert_lines), "result_json": result_path,
            "output_lines": [line for line in lines if line.startswith(("REHEARSAL ", "SAFETY ", "RESULT_JSON="))]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", default=DEFAULT_DATE)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--days", type=int, default=5)
    args = parser.parse_args()

    report: dict[str, Any] = {"card": "C611", "trade_date": args.trade_date,
                              "database": args.database, "started_at": datetime.now(TZ).isoformat(),
                              "checks": {}, "status": "FAIL"}
    try:
        if not os.environ.get("REDIS_URL"):
            raise RuntimeError("REDIS_URL_required")
        from engine_core.data_pipeline.trade_calendar import TradeCalendarStore
        from engine_core.data_pipeline.td_ladder_sink import TaosLadderStore
        from engine_core.data_pipeline.td_sink import TaosStore
        from engine_core.data_pipeline.limit_up_detail_td import DATA_COLUMNS, TaosLimitUpDetailStore
        from engine_core.guardian.limit_up_detail_d1 import run_limit_up_detail_gapfill

        calendar_dates = [day for day in TradeCalendarStore().all_trading_dates() if day <= args.trade_date]
        if args.trade_date not in calendar_dates:
            raise RuntimeError("trade_date_not_in_core_calendar")
        dates = calendar_dates[-max(1, args.days):]
        store = TaosLimitUpDetailStore(args.database)
        result = run_limit_up_detail_gapfill(args.trade_date, store=store, force_dates=dates,
                                             depth=len(dates), include_rows=True)
        expected_by_day = result.get("expected_rows", {})
        if not isinstance(expected_by_day, dict):
            raise RuntimeError("gapfill_missing_expected_rows")
        readback_ok, readback = _check_roundtrip(dates, expected_by_day, store, DATA_COLUMNS)
        report["checks"]["td_readback"] = {"pass": readback_ok, **readback,
                                           "versions": {day: result.get("versions", {}).get(day)
                                                        for day in dates}}
        if not readback_ok or any(readback["row_counts"][day]["readback"] <= 0 for day in dates):
            raise RuntimeError("td_readback_failed")

        kline_store, ladder_store = TaosStore("core_data"), TaosLadderStore("core_data")
        crosscheck: dict[str, Any] = {"kline_mismatch_count": 0, "kline_missing_count": 0,
                                     "kline_compared_symbol_count": 0, "streak_mismatch_count": 0,
                                     "reasons": []}
        for day in [args.trade_date]:
            expected_rows = expected_by_day[day]
            symbols = sorted(expected_rows)
            kline = kline_store.read_day_rows(day, symbols)
            ladder = ladder_store.read_day_rows(day, symbols)
            for symbol, item in expected_rows.items():
                bar = kline.get(symbol)
                if bar is None:
                    crosscheck["kline_missing_count"] += 1
                    crosscheck["reasons"].append({"trade_date": day, "symbol": symbol,
                                                  "check": "kline", "reason": "same_day_daily_kline_missing"})
                else:
                    crosscheck["kline_compared_symbol_count"] += 1
                    for field, source_field, tolerance in (("price", "close", Decimal("0.01")),
                                                            ("trade_amount_yuan", "amount_yuan", Decimal("100")),
                                                            ("pct_chg", "pct_chg", Decimal("0.02"))):
                        try:
                            delta = abs(Decimal(str(item.get(field))) - Decimal(str(bar.get(source_field))))
                        except (InvalidOperation, TypeError):
                            delta = Decimal("Infinity")
                        if delta > tolerance:
                            crosscheck["kline_mismatch_count"] += 1
                            crosscheck["reasons"].append({"trade_date": day, "symbol": symbol, "check": field,
                                                          "reason": "daily_kline_value_differs", "delta": str(delta)})
                derived = ladder.get(symbol)
                if derived is None:
                    crosscheck["streak_mismatch_count"] += 1
                    crosscheck["reasons"].append({"trade_date": day, "symbol": symbol,
                                                  "check": "lb_days", "reason": "ladder_streak_missing"})
                elif item.get("lb_days") is not None and derived.get("streak_n") is not None and \
                        int(item["lb_days"]) != int(derived["streak_n"]):
                    crosscheck["streak_mismatch_count"] += 1
                    crosscheck["reasons"].append({"trade_date": day, "symbol": symbol, "check": "lb_days",
                                                  "reason": "kaipan_vs_daily_kline_derived_streak_differs",
                                                  "kaipan_lb_days": item.get("lb_days"),
                                                  "daily_kline_streak_n": derived.get("streak_n")})
        report["checks"]["daily_kline_and_streak"] = crosscheck
        if crosscheck["kline_mismatch_count"]:
            raise RuntimeError("daily_kline_crosscheck_failed")

        import redis
        redis_client = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True,
                                            socket_connect_timeout=5, socket_timeout=10)
        ttl_expected = _registry_ttl()
        redis_results = {}
        # The hot view is the requested D-1 snapshot; history for the other forced dates is TD-only.
        for day in [args.trade_date]:
            detail_key = f"core:yest_limit_detail:{day}"
            reason_key = f"core:limit_up_reason:{day}"
            detail_count = redis_client.hlen(detail_key)
            reason_count = redis_client.hlen(reason_key)
            detail_ttl, reason_ttl = redis_client.ttl(detail_key), redis_client.ttl(reason_key)
            detail_fields = redis_client.hgetall(detail_key)
            reason_fields = redis_client.hgetall(reason_key)
            ttl_low, ttl_high = ttl_expected * 0.95, ttl_expected * 1.05
            passed = (detail_count == len(expected_by_day[day]) and reason_count == detail_count and
                      set(detail_fields) == set(expected_by_day[day]) and set(reason_fields) == set(detail_fields) and
                      ttl_low <= detail_ttl <= ttl_high and ttl_low <= reason_ttl <= ttl_high)
            redis_results[day] = {"pass": passed, "detail_hash_fields": detail_count,
                                  "reason_hash_fields": reason_count, "detail_ttl": detail_ttl,
                                  "reason_ttl": reason_ttl, "ttl_expected": ttl_expected}
        report["checks"]["redis_readback"] = redis_results
        if not all(item["pass"] for item in redis_results.values()):
            raise RuntimeError("redis_readback_failed")

        report["checks"]["guardian_rehearsal"] = _rehearse(args.database)
        rehearsal = report["checks"]["guardian_rehearsal"]
        if rehearsal.get("return_code") != 0 or rehearsal.get("status") != "SUCCEEDED" or \
                rehearsal.get("ledger_rows", 0) < 1 or rehearsal.get("alerts") != 0 or rehearsal.get("alert_lines"):
            raise RuntimeError("guardian_rehearsal_failed")
        report["status"] = "PASS"
    except Exception as exc:  # acceptance output is safe to publish and never includes connection URLs.
        report["failure"] = f"{type(exc).__name__}:{str(exc)[:180]}"
    finally:
        report["finished_at"] = datetime.now(TZ).isoformat()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
