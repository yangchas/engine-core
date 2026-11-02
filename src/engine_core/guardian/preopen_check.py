"""Pre-open self-check (C635): yesterday's limit-up pool, hot plates and daily bars must be there before the open.

Everything the check demands comes from the event args in schedule.json (min rows, accepted statuses, the time the
hot-plate history fetch must have finished, the daily-bar coverage threshold). The daily-bar check IS the remedy:
it runs the per-symbol TD fill, which costs nothing when the day is complete (no login, no request) and fills the
missing symbols and the completion marker otherwise. The other two have their own scheduled producers, so a miss is
reported as a retryable failure and the event's retry ladder checks again until its window closes.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _manifests(catalog: Any, dataset: str, day: str) -> list[dict[str, Any]]:
    entry = catalog.read().get("datasets", {}).get(dataset, {})
    root = Path(getattr(catalog, "root", "."))
    found = []
    for version in entry.get("dates", {}).get(day, []):
        path = Path(str(version.get("manifest") or ""))
        if not path.is_absolute():
            path = root / path
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(manifest, dict):
            found.append(manifest)
    return found


def _status(manifest: Mapping[str, Any]) -> str:
    return str(manifest.get("business_status") or (manifest.get("quality") or {}).get("status") or "").upper()


def _instant(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=SHANGHAI)


def check_catalog_dataset(catalog: Any, dataset: str, day: str, rule: Mapping[str, Any],
                          check_day: str) -> dict[str, Any]:
    """Pass when some version of ``dataset``/``day`` has an accepted status, enough rows and (if asked) was
    captured at or after ``captured_after`` on ``check_day``."""
    manifests = _manifests(catalog, dataset, day)
    if not manifests:
        return {"id": dataset, "day": day, "status": "MISSING", "detail": "no_version"}
    statuses = {str(item).upper() for item in rule.get("statuses", ())}
    min_rows = int(rule.get("min_rows", 1))
    after = rule.get("captured_after")
    threshold = _instant(f"{check_day}T{after}") if after else None
    best = None
    for manifest in manifests:
        rows = int(manifest.get("row_count") or 0)
        captured = _instant(manifest.get("available_at") or manifest.get("captured_at"))
        if statuses and _status(manifest) not in statuses:
            continue
        if rows < min_rows:
            continue
        if threshold is not None and (captured is None or captured < threshold):
            continue
        best = {"status": _status(manifest), "rows": rows, "available_at": str(manifest.get("available_at"))}
    if best is None:
        summary = [{"status": _status(m), "rows": m.get("row_count"), "available_at": str(m.get("available_at"))[:19]}
                   for m in manifests[-3:]]
        return {"id": dataset, "day": day, "status": "DEGRADED",
                "detail": {"versions": len(manifests), "latest": summary,
                           "need": {"statuses": sorted(statuses), "min_rows": min_rows, "captured_after": after}}}
    return {"id": dataset, "day": day, "status": "OK", "detail": best}


def run(request: Mapping[str, Any], root: str | Path, *, dry_run: bool = False,
        fill: Callable[..., dict[str, Any]] | None = None, catalog: Any = None) -> tuple[int, dict[str, Any]]:
    args = request.get("args") if isinstance(request.get("args"), Mapping) else {}
    check_day = str(request.get("trade_date") or "")
    if not check_day:
        return 64, {"contract": 1, "status": "failed", "error_code": "CONFIG_ERROR", "retryable": False,
                    "artifacts": [], "detail": "trade_date_required"}
    checks = args.get("checks") if isinstance(args.get("checks"), Mapping) else {}
    if not checks:
        return 64, {"contract": 1, "status": "failed", "error_code": "CONFIG_ERROR", "retryable": False,
                    "artifacts": [], "detail": "checks_required"}
    from ..prep_trade_day import previous_trade_date
    day = previous_trade_date(root, check_day)
    if not day:
        return 75, {"contract": 1, "status": "failed", "error_code": "PREVIOUS_TRADE_DAY_UNAVAILABLE",
                    "retryable": True, "artifacts": [], "detail": check_day}
    if catalog is None:
        from ..data_pipeline.storage import ArtifactCatalog
        catalog = ArtifactCatalog(root)
    results = []
    for dataset, rule in checks.items():
        if dataset == "daily_kline":
            continue
        results.append(check_catalog_dataset(catalog, dataset, day, rule, check_day))
    if "daily_kline" in checks:
        results.append(_check_daily_kline(root, day, args, checks["daily_kline"], dry_run, fill))
    bad = [item for item in results if item["status"] != "OK"]
    payload = {"check_day": check_day, "checked_day": day, "results": results}
    if not bad:
        return 0, {"contract": 1, "status": "ok", "error_code": "", "retryable": False,
                   "artifacts": [], "detail": payload}
    return 75, {"contract": 1, "status": "failed", "error_code": "PREOPEN_CHECK_FAILED", "retryable": True,
                "artifacts": [], "detail": {**payload, "failed": [item["id"] for item in bad]}}


def _check_daily_kline(root: Any, day: str, args: Mapping[str, Any], rule: Mapping[str, Any], dry_run: bool,
                       fill: Callable[..., dict[str, Any]] | None) -> dict[str, Any]:
    if dry_run:
        return {"id": "daily_kline", "day": day, "status": "OK", "detail": "dry_run_no_fill"}
    if fill is None:
        from ..data_pipeline.daily_kline_fill import run_daily_kline_fill_from_root as fill
    fill_args = {**args, "daily_kline_policy": {"formal_coverage_threshold": rule.get("coverage_threshold", 0.995)},
                 "total_timeout_seconds": rule.get("total_timeout_seconds", 1200)}
    try:
        result = fill(root, day, args=fill_args)
    except Exception as exc:
        return {"id": "daily_kline", "day": day, "status": "DEGRADED",
                "detail": type(exc).__name__ + ":" + str(exc)[:200]}
    status = result.get("status")
    if status in {"NO_GAPS", "COMPLETE"}:
        return {"id": "daily_kline", "day": day, "status": "OK",
                "detail": {"fill": status, "written_symbols": result.get("written_symbols", 0),
                           "coverage": (result.get("coverage") or {}).get(day)}}
    return {"id": "daily_kline", "day": day, "status": "DEGRADED", "detail": {"fill": status}}
