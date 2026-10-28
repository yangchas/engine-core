"""Schedule adapter for the point-in-time today limit-up pool (C582)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from ..data_pipeline import limit_up_today as core
from ..data_pipeline.providers.limit_up_today_sources import (
    SourceUnavailable, WencaiThrottled, authorize_wencai_request, fetch_wencai_today,
    read_q2_snapshot,
)
from ..data_pipeline.storage import ArtifactCatalog

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass
class TodayDeps:
    redis_snapshot: Callable[[str], Mapping[str, Any]] = read_q2_snapshot
    wencai_snapshot: Callable[[str], Mapping[str, Any]] = fetch_wencai_today
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(SHANGHAI))
    # Redis writes remain opt-in while this card's runtime permission is read-only.
    publish_hot: Callable[[Mapping[str, Any], list[dict[str, Any]]], Mapping[str, Any]] | None = None
    publish_td: Callable[[Mapping[str, Any], list[dict[str, Any]]], Mapping[str, Any]] | None = None


def _local_datetime(value: Any, fallback: datetime) -> datetime:
    if value in (None, ""):
        parsed = fallback
    else:
        try:
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise ValueError("sample_time_invalid") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _same_date_universe_names(root: Path, day: str, as_of: datetime) -> Mapping[str, str] | None:
    catalog = ArtifactCatalog(root)
    versions = catalog.read().get("datasets", {}).get("symbol_universe", {}).get("dates", {}).get(day, [])
    available: list[tuple[datetime, Path]] = []
    for item in versions:
        path = Path(str(item.get("manifest") or ""))
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            timestamp = datetime.fromisoformat(str(manifest.get("available_at") or "").replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            timestamp = timestamp.astimezone(SHANGHAI)
            if timestamp <= as_of:
                available.append((timestamp, path))
        except (OSError, ValueError, TypeError):
            continue
    if not available:
        return None
    try:
        rows = catalog.read_rows(max(available, key=lambda item: item[0])[1])
    except (OSError, ValueError, TypeError):
        return None
    names: dict[str, str] = {}
    for row in rows:
        symbol = core.normalize_symbol(row.get("symbol", row.get("code")))
        name = row.get("name", row.get("short_name", row.get("security_name")))
        if symbol and name not in (None, ""):
            names[symbol] = str(name)
    return names


def _manifest(day: str, sample_time: datetime, rows: list[dict[str, Any]], *, provider: str,
              provider_chain: list[str], status: str, attempts: list[dict[str, Any]],
              quality: Mapping[str, Any], details: Mapping[str, Any]) -> dict[str, Any]:
    pct_values = [row.get("pct") for row in rows if row.get("pct") is not None]
    return {
        "schema_version": core.SCHEMA_VERSION,
        "dataset": core.DATASET,
        "trade_date": day,
        "source": provider,
        "provider": provider,
        "provider_chain": provider_chain,
        "provider_attempts": attempts,
        "available_at": sample_time.isoformat(timespec="milliseconds"),
        "fetched_at": sample_time.isoformat(timespec="milliseconds"),
        "as_of": sample_time.isoformat(timespec="milliseconds"),
        "captured_at": sample_time.isoformat(timespec="milliseconds"),
        "business_status": status,
        "degraded": status == "DEGRADED" or provider != "redis_q2",
        "quality": {"coverage": quality.get("coverage"), "expected_basis": quality.get("expected_basis"),
                    "expected_count": quality.get("expected_count"), "missing": quality.get("missing"),
                    "field_completeness": quality.get("field_completeness", {}),
                    "partial": bool(quality.get("partial")), **dict(quality)},
        "field_units": {"symbol": "none", "date": "iso_date", "as_of": "iso_datetime",
                        "pct": "percent", "limit_price": "yuan", "seal_amount": "yuan",
                        "open_count": "count", "first_limit_time": "local_time",
                        "final_limit_time": "local_time", "source_ts": "iso_datetime"},
        "source_dates": {provider: day},
        "source_asof": sample_time.isoformat(timespec="milliseconds"),
        "inputs": {"sample_as_of": sample_time.isoformat(timespec="milliseconds"),
                   "source_chain": provider_chain},
        "sample_scope": None,
        "point_in_time": True,
        "quality_details": dict(details),
        "pct_range": ([min(pct_values), max(pct_values)] if pct_values else None),
    }


def _commit(root: Path, day: str, rows: list[dict[str, Any]], manifest: dict[str, Any],
            *, provider: str, clock: Callable[[], datetime]) -> dict[str, Any]:
    committed = ArtifactCatalog(root).commit(core.DATASET, day, rows, manifest, source=provider,
                                               clock=clock, context={"sample_as_of": manifest["as_of"]})
    return committed


def _publish_hot(deps: TodayDeps, manifest: Mapping[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    if deps.publish_hot is None:
        return {"status": "not_configured"}
    try:
        result = deps.publish_hot(manifest, rows)
        return dict(result) if isinstance(result, Mapping) else {"status": "unknown"}
    except Exception as exc:  # noqa: BLE001 - artifact catalog remains the durable commit point
        return {"status": "failed", "error": type(exc).__name__}


def _publish_td(deps: TodayDeps, manifest: Mapping[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    if deps.publish_td is None:
        return {"status": "not_configured", "target": "core_data.limit_up_pool_today_v1"}
    try:
        result = deps.publish_td(manifest, rows)
        return dict(result) if isinstance(result, Mapping) else {"status": "unknown"}
    except Exception as exc:  # noqa: BLE001 - no TD schema creation is allowed in this card
        return {"status": "failed", "error": type(exc).__name__}


def _redis_sample(day: str, root: Path, deps: TodayDeps, *, as_of: Any = None,
                  fallback_policy: str = "none") -> tuple[int, dict[str, Any]]:
    try:
        snapshot = deps.redis_snapshot(day)
    except SourceUnavailable as exc:
        return 75, {"contract": 1, "status": "failed", "error_code": "Q2_UNAVAILABLE",
                    "retryable": True, "artifacts": [],
                    "detail": {"error": str(exc), "configured_fallback": fallback_policy}}
    except Exception as exc:  # noqa: BLE001
        return 75, {"contract": 1, "status": "failed", "error_code": "Q2_READ_FAILED",
                    "retryable": True, "artifacts": [], "detail": type(exc).__name__}
    sample_time = _local_datetime(as_of, deps.clock())
    names = _same_date_universe_names(root, day, sample_time)
    try:
        built = core.build_redis_pool(day, sample_time, snapshot.get("active_symbols", []),
                                      snapshot.get("hashes", {}), st_names=names)
    except (TypeError, ValueError) as exc:
        return 40, {"contract": 1, "status": "failed", "error_code": "Q2_SNAPSHOT_INVALID",
                    "retryable": False, "artifacts": [], "detail": type(exc).__name__}
    quality = dict(built["quality"])
    active = int(quality["active_count"])
    missing = int(quality["missing_hash"] + quality["missing_quote"] + quality["future_quote"] +
                  quality["stale_quote"])
    coverage = (int(quality["valid_quote"]) / active) if active else None
    complete_zero = not built["rows"] and missing == 0 and active > 0
    if not built["rows"] and not complete_zero:
        return 40, {"contract": 1, "status": "failed", "error_code": "Q2_POOL_EMPTY_INCOMPLETE",
                    "retryable": False, "artifacts": [], "detail": quality}
    st_unavailable = int(quality["st_unavailable"]) > 0
    status = "FORMAL" if built["redis_terminal"] and names is not None and not st_unavailable else (
        "DEGRADED" if st_unavailable or missing else "PROVISIONAL")
    attempts = [{"provider": "redis_q2", "result": "ACCEPTED", "elapsed_ms": 0, "reason": "q2_active_hashes",
                 "row_count": len(built["rows"])}]
    manifest_quality = {"coverage": coverage, "expected_basis": "not_applicable",
                        "expected_count": active, "missing": missing,
                        "field_completeness": {"px": "PARTIAL" if missing else "COMPLETE",
                                               "pc": "PARTIAL" if missing else "COMPLETE",
                                               "seal_amount": "MISSING"},
                        "partial": bool(missing or st_unavailable)}
    manifest = _manifest(day, sample_time, built["rows"], provider="redis_q2", provider_chain=["redis_q2"],
                         status=status, attempts=attempts, quality=manifest_quality,
                         details={"redis_terminal": built["redis_terminal"], "st_filter": "UNAVAILABLE" if st_unavailable else
                                  "AVAILABLE" if names is not None else "UNAVAILABLE",
                                  "seal_amount": "UNAVAILABLE_REDIS_FIELD", "limit_pool": quality})
    committed = _commit(root, day, built["rows"], manifest, provider="redis_q2", clock=deps.clock)
    hot = _publish_hot(deps, committed, built["rows"])
    return (10 if status == "DEGRADED" else 0), {"contract": 1,
            "status": "degraded" if status == "DEGRADED" else "ok", "error_code": "",
            "retryable": False, "artifacts": [committed["manifest_uri"]],
            "detail": {"version": committed["version"], "as_of": committed["as_of"],
                       "row_count": len(built["rows"]), "business_status": status,
                       "redis_terminal": built["redis_terminal"], "fallback": fallback_policy,
                       "hot_publish": hot}}


def _close_confirmation(day: str, root: Path, deps: TodayDeps, args: Mapping[str, Any], *,
                        as_of: Any = None) -> tuple[int, dict[str, Any]]:
    redis_snapshot = None
    redis_built = None
    redis_error = None
    try:
        redis_snapshot = deps.redis_snapshot(day)
    except Exception as exc:  # noqa: BLE001 - WenCai remains the configured close fallback
        redis_error = type(exc).__name__
    request_clock = deps.clock()
    request_time = _local_datetime(None, request_clock)
    if not "15:20:00" <= request_time.strftime("%H:%M:%S") <= "16:00:00":
        return 64, {"contract": 1, "status": "failed", "error_code": "WENCAI_WINDOW_CLOSED",
                    "retryable": False, "artifacts": [],
                    "detail": {"allowed_window": "15:20:00-16:00:00",
                               "request_time": request_time.isoformat(timespec="seconds")}}
    pre_request_time = _local_datetime(as_of, request_clock)
    min_interval = args.get("wencai_min_interval_seconds", 1800)
    max_per_day = args.get("wencai_max_requests_per_day", 1)
    try:
        if args.get("wencai_confirmation") is not True:
            raise ValueError("wencai_confirmation_not_configured")
        throttle = authorize_wencai_request(root, day, pre_request_time,
                                            min_interval_seconds=min_interval,
                                            max_requests_per_day=max_per_day)
        fetched = deps.wencai_snapshot(day)
    except Exception as exc:  # noqa: BLE001
        if redis_snapshot is not None:
            names = _same_date_universe_names(root, day, pre_request_time)
            try:
                failed_query_redis = core.build_redis_pool(
                    day, pre_request_time, redis_snapshot.get("active_symbols", []),
                    redis_snapshot.get("hashes", {}), st_names=names)
            except (TypeError, ValueError):
                failed_query_redis = None
            if failed_query_redis and failed_query_redis["redis_terminal"]:
                quality = dict(failed_query_redis["quality"])
                st_unavailable = int(quality["st_unavailable"]) > 0 or names is None
                status = "DEGRADED" if st_unavailable else "FORMAL"
                attempts = [{"provider": "redis_q2", "result": "ACCEPTED", "elapsed_ms": 0,
                             "reason": "terminal_snapshot_after_wencai_unavailable",
                             "row_count": len(failed_query_redis["rows"])},
                            {"provider": "wencai", "result": "ERROR", "elapsed_ms": 0,
                             "reason": type(exc).__name__, "row_count": 0}]
                missing = int(quality["missing_hash"] + quality["missing_quote"] +
                              quality["future_quote"] + quality["stale_quote"])
                coverage = (int(quality["valid_quote"]) / int(quality["active_count"])) if quality["active_count"] else None
                manifest = _manifest(
                    day, pre_request_time, failed_query_redis["rows"], provider="redis_q2",
                    provider_chain=["redis_q2", "wencai"], status=status, attempts=attempts,
                    quality={"coverage": coverage, "expected_basis": "not_applicable",
                             "expected_count": int(quality["active_count"]), "missing": missing,
                             "field_completeness": {"seal_amount": "MISSING"},
                             "partial": status != "FORMAL"},
                    details={"redis_terminal": True, "st_filter": "UNAVAILABLE" if st_unavailable else "AVAILABLE",
                             "wencai_error": type(exc).__name__, "seal_amount": "UNAVAILABLE_REDIS_FIELD",
                             "limit_pool": quality})
                committed = _commit(root, day, failed_query_redis["rows"], manifest,
                                    provider="redis_q2", clock=deps.clock)
                hot = _publish_hot(deps, committed, failed_query_redis["rows"])
                cold = _publish_td(deps, committed, failed_query_redis["rows"])
                return (0 if status == "FORMAL" else 10), {"contract": 1,
                        "status": "ok" if status == "FORMAL" else "degraded", "error_code": "",
                        "retryable": False, "artifacts": [committed["manifest_uri"]],
                        "detail": {"version": committed["version"], "as_of": committed["as_of"],
                                   "row_count": len(failed_query_redis["rows"]), "business_status": status,
                                   "redis_terminal": True, "wencai_error": type(exc).__name__,
                                   "hot_publish": hot, "td_publish": cold}}
        code = "WENCAI_THROTTLED" if isinstance(exc, WencaiThrottled) else "WENCAI_UNAVAILABLE"
        return (40 if code == "WENCAI_THROTTLED" else 75), {"contract": 1, "status": "failed",
                "error_code": code, "retryable": code != "WENCAI_THROTTLED", "artifacts": [],
                "detail": {"error": str(exc)[:160], "redis_error": redis_error}}
    completion_time = _local_datetime(None, deps.clock())
    sample_time = max(_local_datetime(as_of, request_clock), completion_time)
    wencai = core.parse_wencai_pool(fetched.get("records", []), day)
    if redis_snapshot is not None:
        names = _same_date_universe_names(root, day, sample_time)
        try:
            redis_built = core.build_redis_pool(day, sample_time, redis_snapshot.get("active_symbols", []),
                                                redis_snapshot.get("hashes", {}), st_names=names)
            redis_rows = redis_built["rows"]
        except (TypeError, ValueError) as exc:
            redis_error = type(exc).__name__
            redis_built = None
            redis_rows = None
    else:
        names = None
        redis_rows = None
    merged = core.merge_confirmations(redis_rows, wencai["rows"], day, sample_time)
    if not merged["rows"] and int(fetched.get("reported_total") or 0) != 0:
        return 40, {"contract": 1, "status": "failed", "error_code": "WENCAI_POOL_EMPTY_UNVERIFIED",
                    "retryable": False, "artifacts": [], "detail": {"reported_total": fetched.get("reported_total")}}
    st_unavailable = bool(redis_built and int(redis_built["quality"]["st_unavailable"]) > 0)
    wencai_complete = bool(fetched.get("complete", False) and not fetched.get("truncated", False) and
                           wencai["date_suffix_verified"] and
                           int(fetched.get("reported_total") or 0) == len(wencai["rows"]))
    redis_terminal = bool(redis_built and redis_built["redis_terminal"] and names is not None and not st_unavailable)
    if redis_built is None:
        formal = wencai_complete
    elif redis_terminal:
        formal = not wencai_complete or merged["confirmed"]
    else:
        formal = wencai_complete and merged["confirmed"]
    published_rows = (redis_rows if redis_terminal and not wencai_complete else merged["rows"])
    status = "FORMAL" if formal else ("PROVISIONAL" if redis_built is None else "DEGRADED")
    provider = "redis_q2" if redis_built is not None else "wencai"
    chain = ["redis_q2", "wencai"]
    attempts = []
    if redis_built is not None:
        attempts.append({"provider": "redis_q2", "result": "ACCEPTED", "elapsed_ms": 0,
                         "reason": "close_terminal_snapshot", "row_count": len(redis_rows or [])})
    else:
        attempts.append({"provider": "redis_q2", "result": "ERROR", "elapsed_ms": 0,
                         "reason": redis_error or "unavailable", "row_count": 0})
    attempts.append({"provider": "wencai", "result": "ACCEPTED", "elapsed_ms": 0,
                     "reason": "single_low_frequency_confirmation", "row_count": len(wencai["rows"])})
    row_count = len(published_rows)
    quality = {"coverage": 1.0 if merged["confirmed"] or (redis_built is None and wencai_complete) else None,
               "expected_basis": "not_applicable",
               "expected_count": len(redis_rows) if redis_rows is not None else len(wencai["rows"]),
               "missing": len(merged["redis_only"]) + len(merged["wencai_only"]),
               "field_completeness": {"seal_amount": "PARTIAL" if any(row.get("seal_amount") is not None for row in published_rows) else "MISSING"},
               "partial": not formal}
    manifest = _manifest(day, sample_time, published_rows, provider=provider, provider_chain=chain,
                         status=status, attempts=attempts, quality=quality,
                         details={"redis_terminal": bool(redis_built and redis_built["redis_terminal"]),
                                  "st_filter": "UNAVAILABLE" if names is None or st_unavailable else "AVAILABLE",
                                  "wencai_date_suffixes": wencai["date_suffixes"],
                                  "wencai_date_suffix_verified": wencai["date_suffix_verified"],
                                  "cross_check": {"redis_only": merged["redis_only"],
                                                  "wencai_only": merged["wencai_only"],
                                                  "confirmed": merged["confirmed"]},
                                  "wencai_complete": wencai_complete,
                                  "wencai_reported_total": fetched.get("reported_total"),
                                  "wencai_row_count": len(wencai["rows"]),
                                  "throttle": throttle, "redis_error": redis_error})
    committed = _commit(root, day, published_rows, manifest, provider=provider, clock=deps.clock)
    hot = _publish_hot(deps, committed, published_rows)
    cold = _publish_td(deps, committed, published_rows)
    return (0 if formal else 10), {"contract": 1, "status": "ok" if formal else "degraded",
            "error_code": "", "retryable": False, "artifacts": [committed["manifest_uri"]],
            "detail": {"version": committed["version"], "as_of": committed["as_of"],
                       "row_count": row_count, "business_status": status,
                       "redis_only": merged["redis_only"], "wencai_only": merged["wencai_only"],
                       "request_count": fetched.get("request_count", 1), "truncated": fetched.get("truncated", False),
                       "hot_publish": hot, "td_publish": cold}}


def run(request: Mapping[str, Any], root: str | Path, *, dry_run: bool = False,
        deps: TodayDeps | None = None) -> tuple[int, dict[str, Any]]:
    """Run one schedule-configured sample; only the close-confirm tier may call WenCai."""
    if dry_run:
        return 20, {"contract": 1, "status": "not_applicable", "error_code": "",
                    "retryable": False, "artifacts": [], "detail": "dry_run"}
    args = request.get("args") if isinstance(request.get("args"), Mapping) else {}
    tier = str(args.get("tier") or "redis")
    raw_day = request.get("data_date") or request.get("trade_date") or request.get("target_day")
    try:
        day = date.fromisoformat(str(raw_day)).isoformat()
    except (TypeError, ValueError):
        return 64, {"contract": 1, "status": "failed", "error_code": "CONFIG_ERROR",
                    "retryable": False, "artifacts": [], "detail": "trade_date_required"}
    root_path = Path(root).expanduser()
    deps = deps or TodayDeps()
    if tier == "redis":
        return _redis_sample(day, root_path, deps, as_of=request.get("as_of"),
                             fallback_policy=str(args.get("failure_fallback", "none")))
    if tier == "close_confirm":
        return _close_confirmation(day, root_path, deps, args, as_of=request.get("as_of"))
    return 64, {"contract": 1, "status": "failed", "error_code": "CONFIG_ERROR",
                "retryable": False, "artifacts": [], "detail": "tier_invalid"}
