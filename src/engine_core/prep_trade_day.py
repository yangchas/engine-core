"""Readiness report and idempotent execution for one trade day's premarket inputs."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .data_pipeline.storage import ArtifactCatalog


def previous_trade_date(root: str | Path, trade_date: str, *,
                        known_trade_dates: Iterable[str] | None = None) -> str | None:
    root = Path(root)
    candidates: set[str] = set(known_trade_dates or ())
    if known_trade_dates is None:
        calendar = root / "trade_dates.json"
        try:
            payload = json.loads(calendar.read_text(encoding="utf-8"))
            candidates.update(payload if isinstance(payload, list) else payload.get("trading_dates", []))
        except (OSError, ValueError, TypeError):
            pass
        if not candidates:
            catalog = ArtifactCatalog(root).read().get("datasets", {})
            candidates.update(catalog.get("daily_kline", {}).get("dates", {}).keys())
    prior = sorted(day for day in candidates if day < trade_date)
    return prior[-1] if prior else None


def _pit_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone() if parsed.tzinfo else None
    except ValueError:
        return None


def _manifest(catalog: ArtifactCatalog, dataset: str, day: str | None = None,
              *, asof: str) -> dict[str, Any] | None:
    path = catalog.locate(dataset, day)
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        available = _pit_time(value.get("available_at"))
        cutoff = _pit_time(asof)
        indexed_versions = catalog.read().get("datasets", {}).get(dataset, {}).get("dates", {}).get(day, [])
        indexed = any(item.get("version") == value.get("version") for item in indexed_versions
                      if isinstance(item, dict))
        if (int(value.get("row_count", 0)) <= 0 or
                value.get("quality", {}).get("status") == "FAILED" or
                not isinstance(value.get("version"), str) or not value.get("version") or
                not indexed or
                available is None or cutoff is None or available > cutoff):
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None


def _run(repo: Path, script: str, args: list[str]) -> tuple[int, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo / "src") + os.pathsep + str(repo)
    if env.get("REDIS_URL") and not env.get("CORE_REDIS_URL"):
        env["CORE_REDIS_URL"] = env["REDIS_URL"]
    result = subprocess.run([sys.executable, str(repo / "examples" / script), *args],
                            cwd=repo, env=env, capture_output=True, text=True, check=False)
    output = (result.stdout + result.stderr).strip()
    return result.returncode, output[-1200:]


def _step(name: str, dataset: str, day: str | None, catalog: ArtifactCatalog,
          *, asof: str, reason: str | None = None) -> dict[str, Any]:
    started = time.monotonic()
    manifest = _manifest(catalog, dataset, day, asof=asof) if day else None
    quality = manifest.get("quality") if manifest and isinstance(manifest.get("quality"), dict) else {}
    if manifest:
        status = ("PARTIAL" if manifest.get("business_status") == "PARTIAL" or
                  quality.get("status") == "PARTIAL" else "READY")
    else:
        status = "SKIPPED_REASON" if reason else "MISSING"
        if day and catalog.locate(dataset, day) is not None:
            reason = "ARTIFACT_NOT_PIT_READY"
    actual_reason = reason
    if manifest and status == "PARTIAL":
        actual_reason = quality.get("missing") or "SOURCE_WINDOW_INCOMPLETE"
    return {"step": name, "dataset": dataset, "trade_date": day, "status": status,
            "reason": actual_reason, "row_count": manifest.get("row_count") if manifest else None,
            "version": manifest.get("version") if manifest else None,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}


def _latest_step(name: str, dataset: str, before: str, catalog: ArtifactCatalog,
                 *, asof: str) -> dict[str, Any]:
    indexed = catalog.read().get("datasets", {}).get(dataset, {}).get("dates", {})
    days = sorted(day for day in indexed if day <= before and _manifest(catalog, dataset, day, asof=asof))
    item = _step(name, dataset, days[-1] if days else None, catalog, asof=asof,
                 reason=(None if days else "no_available_capture_on_or_before_previous_trade_date"))
    if days:
        item["status"] = "READY" if days[-1] == before else "PARTIAL"
        item["reason"] = None if days[-1] == before else "using_latest_available_capture"
    return item


def build_prep_report(root: str | Path, trade_date: str, *, write: bool = False,
                      asof: str | None = None) -> dict[str, Any]:
    date.fromisoformat(trade_date)
    asof = asof or datetime.now().astimezone().isoformat()
    if _pit_time(asof) is None:
        raise ValueError("invalid_preparation_asof")
    root = Path(root).expanduser()
    catalog = ArtifactCatalog(root)
    previous = previous_trade_date(root, trade_date)
    repo = Path(__file__).resolve().parents[2]
    steps = [
        _step("previous_daily_kline", "daily_kline", previous, catalog, asof=asof,
              reason="previous_trade_date_unavailable" if previous is None else None),
        _step("daily_indicators", "daily_indicators", previous, catalog, asof=asof,
              reason="previous_trade_date_unavailable" if previous is None else None),
        _step("daily_report_features", "daily_report_features", previous, catalog, asof=asof,
              reason="previous_trade_date_unavailable" if previous is None else None),
        _step("yesterday_limit_pool", "limit_up_pool", previous, catalog, asof=asof,
              reason="previous_trade_date_unavailable" if previous is None else None),
        _step("limit_reasons", "limit_reasons", previous, catalog, asof=asof,
              reason="previous_trade_date_unavailable" if previous is None else None),
        _step("hot_plates", "hot_plates", previous, catalog, asof=asof,
              reason="previous_trade_date_unavailable" if previous is None else None),
        _latest_step("hot_rank", "hot_rank", trade_date, catalog, asof=asof),
        _step("plate_members", "plate_members", previous, catalog, asof=asof,
              reason="previous_trade_date_unavailable" if previous is None else None),
        _step("auction_baseline", "auction_baseline", trade_date, catalog, asof=asof,
              reason="previous_trade_date_unavailable" if previous is None else None),
        {"step": "monday_live_auction_inputs", "dataset": "auction_snapshot_v2", "trade_date": trade_date,
         "status": "SKIPPED_REASON", "reason": "待交易日09:15起产生，盘前准备不读取未来竞价数据",
         "row_count": None, "version": None, "elapsed_ms": 0.0},
    ]
    errors: list[str] = []
    timings: dict[str, float] = {}
    if write and previous:
        # Reuse committed input when present; each existing provider/job owns cache and quality rules.
        if not _manifest(catalog, "daily_kline", previous, asof=asof):
            started = time.monotonic()
            rc, out = _run(repo, "run_data_jobs.py", ["--job", "daily_kline", "--date", previous,
                "--write", "--artifact-root", str(root)])
            timings["previous_daily_kline"] = round((time.monotonic() - started) * 1000, 3)
            if rc:
                errors.append("daily_kline:COMMAND_FAILED")
        if _manifest(catalog, "daily_kline", previous, asof=asof) and (
                not _manifest(catalog, "daily_indicators", previous, asof=asof)
                or not _manifest(catalog, "daily_report_features", previous, asof=asof)):
            started = time.monotonic()
            from .daily_history import reindex_daily_indicators, materialize_daily_report_features
            try:
                redis = None
                if os.getenv("REDIS_URL"):
                    import redis as redis_package
                    redis = redis_package.Redis.from_url(os.environ["REDIS_URL"], decode_responses=False)
                start_days = sorted(ArtifactCatalog(root).read().get("datasets", {}).get(
                    "daily_kline", {}).get("dates", {}))
                start = next((d for d in reversed(start_days) if d <= previous), previous)
                if not _manifest(catalog, "daily_indicators", previous, asof=asof):
                    reindex_daily_indicators(catalog, start=start, end=previous, redis=redis)
                if not _manifest(catalog, "daily_report_features", previous, asof=asof):
                    materialize_daily_report_features(catalog, trade_date=previous,
                        asof=f"{previous}T23:59:59+08:00", redis=redis)
            except Exception as exc:
                errors.append("daily_features:" + str(exc)[:240])
            elapsed = round((time.monotonic() - started) * 1000, 3)
            timings["daily_indicators"] = elapsed
            timings["daily_report_features"] = elapsed
        for job, dataset in (("limit_up_pool", "limit_up_pool"),
                             ("limit_reasons", "limit_reasons"),
                             ("hot_plates", "hot_plates"),
                             ("plate_members", "plate_members")):
            if not _manifest(catalog, dataset, previous, asof=asof):
                started = time.monotonic()
                extra = ["--max-requests", "15"]
                rc, out = _run(repo, "run_data_jobs.py", ["--job", job, "--date", previous,
                    "--write", "--artifact-root", str(root), *extra])
                timings[{"limit_up_pool": "yesterday_limit_pool", "limit_reasons": "limit_reasons",
                         "hot_plates": "hot_plates", "plate_members": "plate_members"}[dataset]] = round(
                             (time.monotonic() - started) * 1000, 3)
                if rc:
                    errors.append(job + ":COMMAND_FAILED")
        if not _latest_step("hot_rank", "hot_rank", trade_date, catalog, asof=asof)["trade_date"]:
            started = time.monotonic()
            rc, out = _run(repo, "capture_ths_hot_rank.py", ["--execute", "--artifact-root", str(root)])
            timings["hot_rank"] = round((time.monotonic() - started) * 1000, 3)
            if rc:
                errors.append("hot_rank:COMMAND_FAILED")
        started = time.monotonic()
        rc, out = _run(repo, "build_auction_baseline.py", ["--date", trade_date, "--write",
            "--artifact-root", str(root)])
        timings["auction_baseline"] = round((time.monotonic() - started) * 1000, 3)
        if rc:
            errors.append("auction_baseline:COMMAND_FAILED:" + out[-240:])
    # Re-read after writes and annotate each real artifact state.
    if write:
        fresh = ArtifactCatalog(root)
        for item in steps:
            dataset, day = item["dataset"], item.get("trade_date")
            if item["status"] == "SKIPPED_REASON":
                continue
            if item["step"] == "hot_rank":
                revised = _latest_step("hot_rank", "hot_rank", trade_date, fresh, asof=asof)
            else:
                revised = _step(item["step"], dataset, day, fresh, asof=asof,
                                reason=item.get("reason") if not day else None)
            item.update(revised)
            if item["step"] in timings:
                item["elapsed_ms"] = timings[item["step"]]
    return {"trade_date": trade_date, "previous_trade_date": previous,
            "mode": "WRITE" if write else "DRY_RUN", "steps": steps,
            "errors": errors,
            "status": "READY" if all(s["status"] == "READY" for s in steps) and not errors else "PARTIAL"}
