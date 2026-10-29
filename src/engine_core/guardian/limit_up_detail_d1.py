"""Kaipan limit-up detail collection, bounded historical gapfill, TD sink, and Redis hot views (C611)."""
from __future__ import annotations

import importlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from engine_core.data_pipeline.limit_up_detail import build_limit_up_detail_rows
from engine_core.data_pipeline.limit_up_detail_td import DATA_COLUMNS, TaosLimitUpDetailStore
from engine_core.data_pipeline.trade_calendar import TradeCalendarStore

ROOT = Path(__file__).resolve().parents[3]
TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_DATABASE = "core_data"
DEFAULT_DEPTH = 20
MAX_LEVELS = 10


class LimitUpDetailUnavailable(RuntimeError):
    """Kaipan could not supply a date-evidenced limit-up pool; Guardian should retry the same rung."""


class LazyKaipanClient:
    """Load pykaipan only when the Guardian event starts; retain the source APIs' raw response bodies."""

    def _sdk(self):
        return importlib.import_module("pykaipan.pykaipan")

    def get_his_bans(self, day: str, level: int) -> Any:
        return self._sdk().getHisBans(date=day, ban=str(level), size="2000")

    def get_ban_reasons(self, symbol: str, *, st: int = 200) -> Any:
        from engine_core.data_pipeline.providers.kaipan_sdk_adapter import get_ban_reasons
        return get_ban_reasons(symbol, st=st)


def _pool_row_count(response: Mapping[str, Any]) -> int:
    info = response.get("info")
    if not isinstance(info, (list, tuple)):
        return 0
    count = 0
    for segment in info:
        if isinstance(segment, (list, tuple)):
            if len(segment) == 23 and not any(isinstance(item, (list, tuple, dict)) for item in segment):
                count += 1
            else:
                count += sum(1 for row in segment if isinstance(row, (list, tuple)))
    return count


def fetch_raw_day(trade_date: str, client: Any | None = None) -> dict[str, Any]:
    """Fetch all getHisBans levels and each pool member's raw getBanReasons response."""
    client = client or LazyKaipanClient()
    pool_responses: list[Mapping[str, Any]] = []
    request_count = 0
    for level in range(1, MAX_LEVELS + 1):
        request_count += 1
        try:
            response = client.get_his_bans(trade_date, level)
        except Exception as exc:
            if level > 1 and type(exc).__name__ == "TypeError":
                break
            raise LimitUpDetailUnavailable("kaipan_getHisBans_failed:" + type(exc).__name__) from None
        if not isinstance(response, Mapping):
            if level > 1 and response in (None, [], {}):
                break
            raise LimitUpDetailUnavailable("kaipan_getHisBans_shape_invalid")
        errcode = str(response.get("errcode", "0"))
        if errcode != "0":
            if level > 1 and errcode == "1020":
                break
            raise LimitUpDetailUnavailable("kaipan_getHisBans_errcode:" + errcode[:24])
        if not isinstance(response.get("info"), (list, tuple)):
            if level > 1:
                break
            raise LimitUpDetailUnavailable("kaipan_getHisBans_info_missing")
        pool_responses.append(response)
        if _pool_row_count(response) == 0:
            break
    if not pool_responses or not any(_pool_row_count(response) for response in pool_responses):
        raise LimitUpDetailUnavailable("kaipan_getHisBans_no_rows")

    # Build once to validate the request-date echo and obtain the exact, de-duplicated symbol set before reasons.
    observed_at = datetime.now(TZ).isoformat(timespec="seconds")
    preliminary = build_limit_up_detail_rows(trade_date, pool_responses, {}, captured_at=observed_at)
    reason_responses: dict[str, Any] = {}
    reason_captured_at: dict[str, str] = {}
    reason_errors = 0
    for row in preliminary:
        request_count += 1
        try:
            reason_responses[row["symbol"]] = client.get_ban_reasons(row["symbol"], st=200)
            reason_captured_at[row["symbol"]] = datetime.now(TZ).isoformat(timespec="seconds")
        except Exception:
            reason_errors += 1
    if preliminary and reason_errors == len(preliminary):
        raise LimitUpDetailUnavailable("kaipan_getBanReasons_unavailable")
    captured_at = datetime.now(TZ).isoformat(timespec="seconds")
    rows = build_limit_up_detail_rows(trade_date, pool_responses, reason_responses,
                                      captured_at=captured_at, reason_captured_at=reason_captured_at)
    return {"trade_date": trade_date, "rows": rows, "captured_at": captured_at,
            "pool_response_count": len(pool_responses), "reason_request_count": len(reason_responses),
            "reason_error_count": reason_errors, "request_count": request_count}


def registry_ttl_seconds() -> int:
    registry = json.loads((ROOT / "src/engine_core/guardian/datasets.registry.json").read_text(encoding="utf-8"))
    item = next((entry for entry in registry.get("items", []) if entry.get("id") == "limit_up_detail"), None)
    ttl = int((item or {}).get("redis_ttl_seconds") or 0)
    if ttl <= 0:
        raise ValueError("limit_up_detail_redis_ttl_missing")
    return ttl


def _redis_from_env():
    url = os.environ.get("REDIS_URL")
    if not url:
        raise RuntimeError("REDIS_URL_required")
    import redis
    return redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=5, socket_timeout=10)


def _redis_documents(row: Mapping[str, Any]) -> tuple[str, str]:
    zscodes = json.loads(row.get("reason_zscodes_json") or "[]")
    detail = {
        "trade_date": row.get("trade_date"), "symbol": row.get("symbol"),
        "reason": row.get("reason_text_raw"), "theme": row.get("theme"),
        "lb_days": row.get("lb_days"),
        "times": {"limit_time_epoch": row.get("limit_time_epoch"),
                  "first_seal_time": row.get("first_seal_time"), "last_seal_time": row.get("last_seal_time")},
        "amounts": {"trade_amount_yuan": row.get("trade_amount_yuan"),
                    "seal_amount_yuan": row.get("seal_amount_yuan"),
                    "max_seal_amount_yuan": row.get("max_seal_amount_yuan"),
                    "main_net_yuan": row.get("main_net_yuan"), "main_buy_yuan": row.get("main_buy_yuan"),
                    "turnover_yuan": row.get("turnover_yuan"),
                    "main_sell_signed_yuan": row.get("main_sell_signed_yuan")},
        "quality": {"source_quality": row.get("source_quality"), "detail": row.get("quality_json")},
        "source": row.get("source"), "available_at": row.get("available_at"),
    }
    reason = {"main_plate_code": row.get("main_reason_plate_code"), "all_zscodes": zscodes,
              "reason_source_date": row.get("reason_source_date")}
    return (json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(reason, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def write_redis_views(trade_date: str, rows: Sequence[Mapping[str, Any]], *, redis_client: Any | None = None,
                      ttl_seconds: int | None = None) -> dict[str, Any]:
    """Best-effort hot view write: Redis failure warns and never cancels the completed TD output."""
    client = redis_client
    detail_key = f"core:yest_limit_detail:{trade_date}"
    reason_key = f"core:limit_up_reason:{trade_date}"
    result: dict[str, Any] = {"status": "WARN", "detail_key": detail_key, "reason_key": reason_key,
                              "row_count": len(rows)}
    try:
        client = client or _redis_from_env()
        ttl = int(ttl_seconds or registry_ttl_seconds())
        if ttl <= 0:
            raise ValueError("redis_ttl_invalid")
        detail_values, reason_values = {}, {}
        for row in rows:
            symbol = str(row.get("symbol") or "")
            detail_values[symbol], reason_values[symbol] = _redis_documents(row)
        _remove_stale_hash_fields(client, detail_key, set(detail_values))
        _remove_stale_hash_fields(client, reason_key, set(reason_values))
        client.hset(detail_key, mapping=detail_values)
        client.expire(detail_key, ttl)
        client.hset(reason_key, mapping=reason_values)
        client.expire(reason_key, ttl)
        detail_exists, reason_exists = bool(client.exists(detail_key)), bool(client.exists(reason_key))
        detail_count, reason_count = int(client.hlen(detail_key)), int(client.hlen(reason_key))
        detail_ttl, reason_ttl = int(client.ttl(detail_key)), int(client.ttl(reason_key))
        ok = (detail_exists and reason_exists and detail_count == len(rows) and reason_count == len(rows)
              and detail_ttl > 0 and reason_ttl > 0)
        result.update({"status": "OK" if ok else "WARN", "detail_exists": detail_exists,
                       "reason_exists": reason_exists, "detail_fields": detail_count,
                       "reason_fields": reason_count, "detail_ttl": detail_ttl, "reason_ttl": reason_ttl,
                       "ttl_expected": ttl})
        if not ok:
            result["warning"] = "redis_readback_mismatch"
    except Exception as exc:
        result["warning"] = "redis_write_or_readback_failed:" + type(exc).__name__
    return result


def _remove_stale_hash_fields(client: Any, key: str, expected_fields: set[str]) -> None:
    """Reconcile only C611 core hashes; never delete keys or touch legacy namespaces."""
    stale = set(client.hkeys(key)) - expected_fields
    if stale:
        client.hdel(key, *sorted(stale))


def _quality(rows: Sequence[Mapping[str, Any]], reason_error_count: int = 0) -> dict[str, Any]:
    matched = sum(1 for row in rows if row.get("reason_source_date") == row.get("trade_date"))
    historical = sum(1 for row in rows if row.get("source_quality") == "DEGRADED")
    unmatched = len(rows) - matched - historical
    status = ("FORMAL" if matched == len(rows) and not reason_error_count else
              "DEGRADED" if historical and unmatched == 0 and not reason_error_count else "PARTIAL")
    return {"status": status, "row_count": len(rows), "reason_matched_count": matched,
            "reason_historical_count": historical, "reason_unmatched_count": unmatched,
            "reason_request_error_count": reason_error_count,
            "parser_version": "c611-theme-v1", "source_date_evidence": "getHisBans.info_date_echo"}


def _rows_by_symbol(rows: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    entries = rows.items() if isinstance(rows, Mapping) else ((None, row) for row in rows)
    for key, row in entries:
        if not isinstance(row, Mapping):
            raise ValueError("limit_up_detail_row_invalid")
        symbol = str(row.get("symbol") or key or "")
        if not symbol:
            raise ValueError("limit_up_detail_symbol_missing")
        result[symbol] = {**{column: row.get(column) for column in DATA_COLUMNS}, "symbol": symbol}
    return result


def run_limit_up_detail_gapfill(trade_date: str, *, database: str = DEFAULT_DATABASE,
                                store: TaosLimitUpDetailStore | None = None, client: Any | None = None,
                                redis_client: Any | None = None, force_dates: Sequence[str] = (),
                                depth: int = DEFAULT_DEPTH, root: str | Path | None = None,
                                publish_redis: bool = True, include_rows: bool = False) -> dict[str, Any]:
    """Backfill missing trading dates (newest first), capped at ``depth``; the request echo is checked per day."""
    calendar = TradeCalendarStore(root)
    dates = [day for day in calendar.all_trading_dates() if day <= trade_date]
    if trade_date not in dates:
        raise ValueError("limit_up_detail_trade_date_not_in_calendar")
    candidates = list(reversed(dates[-max(1, int(depth)):]))
    forced = {str(day) for day in force_dates}
    if not forced.issubset(set(candidates)):
        raise ValueError("limit_up_detail_force_date_outside_depth_or_calendar")
    target_store = store or TaosLimitUpDetailStore(database)
    target_store.ensure_schema()
    existing = target_store.existing_day_counts()  # one grouped query for the candidate window
    expected_rows: dict[str, dict[str, dict[str, Any]]] = {}
    versions: dict[str, str | None] = {}
    days: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for day in candidates:
        if existing.get(day, 0) > 0 and day not in forced:
            stored = target_store.read_day_rows(day)
            expected_rows[day] = _rows_by_symbol(stored)
            versions_for_day = target_store.read_versions(day)
            versions[day] = versions_for_day[-1].get("version") if versions_for_day else None
            days[day] = {"status": "EXISTING", "row_count": len(stored), "version": versions[day]}
            continue
        collected = fetch_raw_day(day, client)
        rows = collected["rows"]
        if not rows:
            days[day] = {"status": "EMPTY_UNKNOWN", "row_count": 0, "written": False}
            warnings.append(day + ":empty_source_not_written")
            continue
        reason_errors = int(collected["reason_error_count"])
        quality = _quality(rows, reason_errors)
        versions_before = target_store.read_versions(day)
        prior_first = next((str(item.get("first_observed_at")) for item in versions_before
                            if item.get("first_observed_at")), None)
        persisted = target_store.store_day(day, rows, captured_at=collected["captured_at"],
                                           first_observed_at=prior_first or collected["captured_at"],
                                           quality=quality, business_status=quality["status"])
        expected_rows[day] = _rows_by_symbol(rows)
        versions[day] = persisted["version"]
        days[day] = {"status": "WRITTEN", **persisted, "reason_matched_count": quality["reason_matched_count"],
                     "reason_error_count": reason_errors, "request_count": collected["request_count"]}
        if reason_errors:
            warnings.append(day + f":getBanReasons_errors={reason_errors}")
    redis_result: dict[str, Any] | None = None
    target_rows = expected_rows.get(trade_date)
    if publish_redis and target_rows:
        redis_result = write_redis_views(trade_date, list(target_rows.values()), redis_client=redis_client)
        if redis_result.get("status") != "OK":
            warnings.append("redis_hot_view_write_failed")
    elif publish_redis:
        warnings.append("redis_hot_view_target_not_available")
    result = {"status": "OK" if target_rows else "WAITING", "trade_date": trade_date,
              "depth": max(1, int(depth)), "selected_dates": candidates, "days": days,
              "versions": versions, "redis": redis_result,
              "warnings": warnings, "existing_dates_count": len(existing)}
    if include_rows:
        result["expected_rows"] = expected_rows
    return result


def run(request: Mapping[str, Any], root: str | Path, *, dry_run: bool = False) -> tuple[int, dict[str, Any]]:
    args = request.get("args") if isinstance(request.get("args"), Mapping) else {}
    trade_date = str(request.get("data_date") or request.get("trade_date") or "")
    try:
        result = run_limit_up_detail_gapfill(
            trade_date, database=str(args.get("database") or os.environ.get("CORE_TD_DATABASE") or DEFAULT_DATABASE),
            depth=int(args.get("backfill_depth", DEFAULT_DEPTH)), root=root,
            publish_redis=not dry_run,
        )
    except LimitUpDetailUnavailable as exc:
        return 75, {"contract": 1, "status": "failed", "error_code": "KAIPAN_LIMIT_DETAIL_WAITING",
                    "retryable": True, "artifacts": [], "detail": str(exc)}
    except Exception as exc:
        return 40, {"contract": 1, "status": "failed", "error_code": "LIMIT_UP_DETAIL_FAILED",
                    "retryable": False, "artifacts": [], "detail": f"{type(exc).__name__}:{str(exc)[:180]}"}
    if result["status"] != "OK":
        return 75, {"contract": 1, "status": "failed", "error_code": "KAIPAN_LIMIT_DETAIL_WAITING",
                    "retryable": True, "artifacts": [], "detail": result}
    return 0, {"contract": 1, "status": "ok", "error_code": "", "retryable": False,
               "artifacts": [], "detail": result}
