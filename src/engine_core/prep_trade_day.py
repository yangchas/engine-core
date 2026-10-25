"""Readiness report and idempotent execution for one trade day's premarket inputs."""
from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .data_pipeline.storage import ArtifactCatalog
from .data_pipeline.trade_calendar import TradeCalendarStore
from .data_pipeline.job_contract import parse as parse_job_result


def previous_trade_date(root: str | Path, trade_date: str, *,
                        known_trade_dates: Iterable[str] | None = None) -> str | None:
    from .trade_day_context import previous_trade_date as context_previous
    try:
        return context_previous(root, trade_date)
    except ValueError:
        return None


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


def build_and_commit_stock_theme_assignment(
        catalog: ArtifactCatalog, *, trade_date: str, previous_trade_date: str,
        pool_labels: dict[str, list[dict[str, str]]], reason_rows: list[dict[str, Any]],
        static_themes: dict[str, list[dict[str, str]]],
        hot_plate_names: dict[str, list[dict[str, str]]],
        members: dict[str, list[str]], trading_dates: Iterable[str],
        available_at: str, source_versions: dict[str, Any] | None, as_of: str,
        row_prefix: dict[str, Any] | None = None,
        row_metadata: dict[str, Any] | None = None,
        manifest_metadata: dict[str, Any] | None = None,
        commit_catalog: bool = True,
        source: str = "prep_trade_day") -> dict[str, Any]:
    """Build and publish a theme assignment from retained inputs only.

    Callers provide provenance and authority metadata so normal preparation
    and late audit rebuilds share the same assignment builder and catalog path.
    """
    from .stock_theme_assignment import build_stock_theme_assignments

    assignment = build_stock_theme_assignments(
        trade_date=trade_date, previous_trade_date=previous_trade_date,
        pool_labels=pool_labels, reason_rows=reason_rows,
        static_themes=static_themes, hot_plate_names=hot_plate_names,
        members=members, trading_dates=trading_dates)
    prefix = row_prefix or {}
    suffix = row_metadata or {}
    rows = [{**prefix, **row, **suffix} for row in assignment.values()]
    manifest = {"trade_date": trade_date, "point_in_time_date": previous_trade_date,
                "used_date": previous_trade_date, "available_at": available_at,
                "as_of": as_of,
                "business_status": "READY", "quality": {"status": "READY"}}
    if source_versions is not None:
        manifest["source_versions"] = source_versions
    manifest.update(manifest_metadata or {})
    if not commit_catalog:
        return {"rows": rows, "manifest": manifest, "row_count": len(rows)}
    return catalog.commit("stock_theme_assignment", trade_date, rows, manifest, source=source)


def _prepare_theme_snapshot_cache(catalog: ArtifactCatalog, trade_date: str,
                                  previous: str, asof: str) -> bool:
    """Build the target-day theme view from PIT-ready Core artifacts only."""
    from .limit_reason_theme import (THEME_SNAPSHOT_NODE_SCHEMA,
                                     load_kaipan_theme_snapshot, theme_snapshot_node_inputs,
                                     theme_snapshot_version)
    from .data_pipeline.node_store import NodeStore
    from .slices import _pit_rows

    def load(dataset: str, day: str) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
        entries = (catalog.read().get("datasets", {}).get(dataset, {})
                   .get("dates", {}).get(day, []))
        cutoff = _pit_time(asof)
        candidates = []
        for entry in entries:
            path = Path(entry.get("manifest") or "")
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            available = _pit_time(manifest.get("available_at"))
            quality = manifest.get("quality")
            failed = isinstance(quality, dict) and quality.get("status") == "FAILED"
            if available is not None and cutoff is not None and available <= cutoff and not failed:
                candidates.append((available, path, manifest))
        if not candidates:
            return None
        _available, path, selected_manifest = max(candidates, key=lambda item: (item[0], str(item[1])))
        try:
            manifest = selected_manifest
            rows = catalog.read_rows(path)
            for row in rows:
                row.setdefault("available_at", manifest.get("available_at"))
            return rows, manifest
        except (OSError, ValueError, TypeError, KeyError):
            return None

    pool = load("limit_up_pool", previous)
    hot = load("hot_plates", previous)
    cutoff = asof
    if pool:
        pool = (_pit_rows(pool[0], cutoff)[0], pool[1])
    if hot:
        hot = (_pit_rows(hot[0], cutoff)[0], hot[1])
    registry_catalog = catalog.read().get("datasets", {}).get("plate_registry", {}).get("dates", {})
    registry_dates = sorted(day for day in registry_catalog if day <= trade_date)
    registry = next((load("plate_registry", day) for day in reversed(registry_dates)
                     if load("plate_registry", day)), None)
    reasons: list[dict[str, Any]] = []
    reason_manifests: dict[str, str] = {}
    reason_dates = sorted(catalog.read().get("datasets", {}).get("limit_reasons", {})
                          .get("dates", {}))
    for day in reason_dates:
        if day > trade_date:
            continue
        loaded = load("limit_reasons", day)
        if loaded:
            rows, manifest = loaded
            reasons.extend(_pit_rows(rows, cutoff)[0])
            reason_manifests[day] = str(manifest.get("version") or "")
    unique_reasons = {}
    for row in reasons:
        key = (row.get("symbol", row.get("code")), row.get("Date", row.get("date")),
               str(row.get("ZSCode", row.get("zs_code", ""))),
               row.get("Reason", row.get("reason")))
        unique_reasons[key] = row
    reasons = list(unique_reasons.values())
    if not pool or not registry or not reasons:
        return False
    registry_rows, registry_manifest = registry
    registry_rows, _registry_unknown = _pit_rows(registry_rows, cutoff)
    plate_names: dict[str, str] = {}
    for row in registry_rows:
        code = str(row.get("plate_code", row.get("code", ""))).strip()
        name = str(row.get("name", "") or "").strip()
        if code and name:
            plate_names[code] = name
    plate_names.setdefault("801827", "AI视频")
    try:
        calendar = TradeCalendarStore(catalog.root)
        previous_session = calendar.previous_trade_date(trade_date)
        calendar_dates = [day for day in calendar.all_trading_dates()
                          if day <= previous_session.isoformat()]
    except ValueError:
        return False
    # Keep a cold, immutable D06 assignment artifact beside the report snapshot.
    # The existing theme snapshot remains for compatibility during migration.
    from .limit_reason_theme import _pool_rows, _pool_values
    pool_labels: dict[str, list[dict[str, str]]] = {}
    for raw in _pool_rows(pool[0]):
        symbol, label, topics, code, _tier = _pool_values(raw)
        candidates = []
        for candidate_code, candidate_name in ([(code, plate_names.get(code, label or code))]
                                                if code else []):
            if candidate_code:
                candidates.append({"id": candidate_code, "name": candidate_name})
        candidates.extend({"id": name, "name": name} for name in topics if name)
        if symbol and candidates:
            pool_labels[symbol] = candidates
    assignment_reasons = []
    for row in reasons:
        normalized = dict(row)
        codes = row.get("ZSCode", row.get("zs_code", []))
        if isinstance(codes, str):
            codes = [codes]
        if isinstance(codes, list):
            normalized["ZSCode"] = [{"id": str(code),
                                     "name": str(plate_names.get(str(code), str(code)))}
                                    for code in codes if str(code).strip()]
        assignment_reasons.append(normalized)
    member_codes: dict[str, set[str]] = {}
    member_rows: list[dict[str, Any]] = []
    members_data = load("plate_members", previous)
    if members_data:
        member_rows, _ = _pit_rows(members_data[0], asof)
        for row in member_rows:
            symbol = str(row.get("symbol", row.get("stock_code", row.get("stockid", row.get("code", "")))) or "")[-6:].zfill(6)
            code = str(row.get("plate_code", row.get("plateid", row.get("code_plate", row.get("plate", "")))) or "").strip()
            if symbol and code:
                member_codes.setdefault(symbol, set()).add(code)
    static_by_symbol = {
        symbol: [{"id": code, "name": plate_names[code]} for code in sorted(codes)
                 if code in plate_names]
        for symbol, codes in member_codes.items()
    }
    hot_by_code = {}
    for row in hot[0] if hot else ():
        code = str(row.get("plate_code", row.get("code", "")) or "").strip()
        name = str(row.get("name", row.get("plate_name", "")) or "").strip()
        if code and name:
            hot_by_code[code] = {"id": code, "name": name}
    hot_by_symbol = {symbol: [hot_by_code[code] for code in sorted(codes) if code in hot_by_code]
                     for symbol, codes in member_codes.items()}
    node_inputs = theme_snapshot_node_inputs(
        trade_date=trade_date, previous_trade_date=previous,
        slices={"yest_limit_pool": (str(pool[1].get("version") or "unknown"), len(pool[0])),
                "plate_registry": (str(registry_manifest.get("version") or "unknown"), len(registry_rows)),
                "plate_members": (str(members_data[1].get("version") or "unknown") if members_data else "unknown",
                                  len(member_rows)),
                "hot_plates_yesterday": (str(hot[1].get("version") or "unknown") if hot else "unknown",
                                          len(hot[0]) if hot else 0),
                "limit_reason_history": (json.dumps(sorted(reason_manifests.items()), separators=(",", ":")),
                                          len(reasons))},
        calendar_dates=calendar_dates, plate_names=plate_names)
    node_store = NodeStore(catalog)
    snapshot, _hit = node_store.compute(
        "theme_snapshot", trade_date, f"{THEME_SNAPSHOT_NODE_SCHEMA}:algorithm-v1", node_inputs,
        lambda: load_kaipan_theme_snapshot(
            None, report_date=trade_date, previous_trade_date=previous, observed_at=asof,
            plate_names=plate_names, static_themes_by_symbol={}, pool_rows=pool[0],
            reason_rows=reasons, hot_rows=hot[0] if hot else (), trading_dates=calendar_dates),
        asof=asof)
    if snapshot.get("status") not in {"READY", "PARTIAL"}:
        return False
    input_version = theme_snapshot_version(node_inputs)
    visibility_sha256 = input_version
    build_and_commit_stock_theme_assignment(
        catalog, trade_date=trade_date, previous_trade_date=previous,
        pool_labels=pool_labels, reason_rows=assignment_reasons,
        static_themes=static_by_symbol, hot_plate_names=hot_by_symbol,
        members={symbol: sorted(codes | {item["id"] for item in pool_labels.get(symbol, [])})
                 for symbol, codes in member_codes.items()} |
                {symbol: [item["id"] for item in candidates]
                 for symbol, candidates in pool_labels.items()},
        trading_dates=calendar_dates, available_at=asof,
        source_versions=None,
        as_of=asof,
        row_prefix={"trade_date": trade_date, "previous_trade_date": previous,
                    "used_date": previous, "as_of": asof,
                    "input_version": input_version,
                    "visibility_sha256": visibility_sha256},
        manifest_metadata={"input_version": input_version, "used_date": previous,
                           "visibility_sha256": visibility_sha256,
                           "authority": "NON_AUTHORITATIVE_AUDIT_ONLY",
                           "report_consumed": False},
        source="prep_trade_day")
    return True


def build_prep_report(root: str | Path, trade_date: str, *, write: bool = False,
                      asof: str | None = None) -> dict[str, Any]:
    date.fromisoformat(trade_date)
    live_default_asof = asof is None
    asof = asof or datetime.now().astimezone().isoformat()
    if _pit_time(asof) is None:
        raise ValueError("invalid_preparation_asof")
    root = Path(root).expanduser()
    catalog = ArtifactCatalog(root)
    previous = previous_trade_date(root, trade_date)
    repo = Path(__file__).resolve().parents[2]
    steps = [
        _step("symbol_universe", "symbol_universe", trade_date, catalog, asof=asof),
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
        _step("plate_amount_baseline", "plate_amount_baseline", trade_date, catalog, asof=asof,
              reason="NO_HISTORICAL_PLATE_MEMBERS" if previous is None else None),
        {"step": "monday_live_auction_inputs", "dataset": "auction_snapshot_v2", "trade_date": trade_date,
         "status": "SKIPPED_REASON", "reason": "待交易日09:15起产生，盘前准备不读取未来竞价数据",
         "row_count": None, "version": None, "elapsed_ms": 0.0},
    ]
    errors: list[str] = []
    timings: dict[str, float] = {}
    symbol_contract_status: str | None = None
    if write and previous:
        existing_universe = _manifest(catalog, "symbol_universe", trade_date, asof=asof)
        existing_degraded = bool(
            existing_universe and type(existing_universe.get("tier")) is int and
            existing_universe["tier"] >= 1 and existing_universe.get("degraded") is True)
        if existing_degraded:
            symbol_contract_status = "degraded"
        if existing_universe is None or existing_degraded:
            started = time.monotonic()
            rc, out = _run(repo, "run_data_jobs.py", ["--job", "symbol_universe", "--date", trade_date,
                "--execute", "--artifact-root", str(root)])
            timings["symbol_universe"] = round((time.monotonic() - started) * 1000, 3)
            symbol_result = parse_job_result(out, rc)
            current_universe = None
            current_path = catalog.locate("symbol_universe", trade_date)
            if current_path is not None:
                try:
                    current_universe = json.loads(current_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    current_universe = None
            current_tier = current_universe.get("tier") if current_universe else None
            current_is_degraded = bool(
                current_universe and type(current_tier) is int and current_tier >= 1 and
                current_universe.get("degraded") is True)
            current_is_primary = bool(
                current_universe and type(current_tier) is int and current_tier == 0 and
                current_universe.get("degraded") is not True)
            if rc == 0 and symbol_result.status == "ok" and current_is_primary:
                symbol_contract_status = "ok"
            elif rc == 10 and symbol_result.status == "degraded" and current_is_degraded:
                symbol_contract_status = "degraded"
            elif rc == 20 and symbol_result.status == "not_applicable":
                if existing_degraded and current_is_degraded:
                    symbol_contract_status = "degraded"
                else:
                    symbol_contract_status = "not_applicable"
            elif existing_degraded:
                # A failed upgrade does not invalidate a still-readable committed version.
                symbol_contract_status = "degraded"
            else:
                import logging
                logging.getLogger(__name__).error("symbol_universe preparation failed: %s", out[-2000:])
                errors.append("symbol_universe:COMMAND_FAILED")
            if symbol_contract_status in {"ok", "degraded"} and live_default_asof:
                # A successful real-time fetch becomes visible at completion, never retroactively.
                asof = datetime.now().astimezone().isoformat()
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
        started = time.monotonic()
        rc, out = _run(repo, "build_plate_amount_baseline.py", ["--date", trade_date, "--write",
            "--artifact-root", str(root)])
        timings["plate_amount_baseline"] = round((time.monotonic() - started) * 1000, 3)
        if rc:
            errors.append("plate_amount_baseline:COMMAND_FAILED:" + out[-240:])
        try:
            if not _prepare_theme_snapshot_cache(fresh_catalog := ArtifactCatalog(root),
                                                 trade_date, previous, asof):
                errors.append("theme_snapshot:PRECOMPUTE_UNAVAILABLE")
            else:
                timings["theme_snapshot"] = 0.0
        except Exception as exc:
            errors.append("theme_snapshot:" + str(exc)[:200])
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
            if item["step"] == "symbol_universe":
                if symbol_contract_status == "degraded":
                    item["status"] = "PARTIAL"
                    item["reason"] = "tier=1 carried_forward"
                elif symbol_contract_status == "ok":
                    item["status"] = "READY"
                    item["reason"] = None
                elif symbol_contract_status == "not_applicable":
                    item["status"] = "SKIPPED_REASON"
                    item["reason"] = "not_applicable"
            if item["step"] in timings:
                item["elapsed_ms"] = timings[item["step"]]
    return {"trade_date": trade_date, "previous_trade_date": previous,
            "mode": "WRITE" if write else "DRY_RUN", "steps": steps,
            "errors": errors,
            "status": "READY" if all(s["status"] == "READY" for s in steps) and not errors else "PARTIAL"}
