"""Readiness report and idempotent execution for one trade day's premarket inputs."""
from __future__ import annotations

import json
import logging
import uuid
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


def _plate_members_snapshot(*, asof: str) -> dict[str, Any] | None:
    """Read the same verified ACTIVE snapshot published by the fill job."""
    from .data_pipeline.reference_snapshot_td import ReferenceSnapshotStore

    store = ReferenceSnapshotStore()
    try:
        value = store.read_latest("plate_members")
    except Exception:  # An unavailable or corrupt snapshot is not ready.
        return None
    finally:
        store.close()
    if not value:
        return None
    try:
        available, cutoff = _pit_time(value.get("available_at")), _pit_time(asof)
        quality = value.get("quality") or {}
        if (int(value.get("row_count", 0)) <= 0 or quality.get("status") == "FAILED" or
                not value.get("version") or available is None or cutoff is None or available > cutoff):
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    return value


def _run(repo: Path, script: str, args: list[str]) -> tuple[int, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo / "src") + os.pathsep + str(repo)
    if env.get("REDIS_URL") and not env.get("CORE_REDIS_URL"):
        env["CORE_REDIS_URL"] = env["REDIS_URL"]
    result = subprocess.run([sys.executable, str(repo / "examples" / script), *args],
                            cwd=repo, env=env, capture_output=True, text=True, check=False)
    output = (result.stdout + result.stderr).strip()
    return result.returncode, output[-1200:]


def _declared_code(output: str) -> str | None:
    """The error code a failed job script printed in its contract line (stderr text may follow it)."""
    from .errors import declared_result, lookup
    for line in reversed(str(output or "").splitlines()):
        declared = declared_result(line)
        if declared is not None:
            code = declared.get("error_code")
            return code if isinstance(code, str) and lookup(code) else None
    return None


def _step(name: str, dataset: str, day: str | None, catalog: ArtifactCatalog,
          *, asof: str, reason: str | None = None) -> dict[str, Any]:
    started = time.monotonic()
    manifest = ((_plate_members_snapshot(asof=asof) if dataset == "plate_members" else
                 _manifest(catalog, dataset, day, asof=asof)) if day else None)
    quality = manifest.get("quality") if manifest and isinstance(manifest.get("quality"), dict) else {}
    if manifest:
        status = ("PARTIAL" if manifest.get("business_status") == "PARTIAL" or
                  quality.get("status") == "PARTIAL" else "READY")
    else:
        status = "SKIPPED_REASON" if reason else "MISSING"
        if day and dataset != "plate_members" and catalog.locate(dataset, day) is not None:
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


def _why_unavailable(catalog: ArtifactCatalog, dataset: str, day: str | None, asof: str) -> str:
    """Name the exact reason a dataset/day is not usable at ``asof`` (C587): absent / not yet available / failed."""
    entries = catalog.read().get("datasets", {}).get(dataset, {}).get("dates", {}).get(day or "", [])
    if not entries:
        return f"{dataset}[{day}]:absent_from_catalog"
    cutoff = _pit_time(asof)
    late, failed, unreadable = [], 0, 0
    for entry in entries:
        try:
            manifest = json.loads(Path(entry.get("manifest") or "").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unreadable += 1
            continue
        quality = manifest.get("quality")
        if isinstance(quality, dict) and quality.get("status") == "FAILED":
            failed += 1
            continue
        available = _pit_time(manifest.get("available_at"))
        if available is None or cutoff is None or available > cutoff:
            late.append(str(manifest.get("available_at")))
    if late:
        return f"{dataset}[{day}]:available_at_after_asof(available_at={min(late)},asof={asof})"
    if failed:
        return f"{dataset}[{day}]:all_versions_failed_quality"
    return f"{dataset}[{day}]:manifest_unreadable"


def _prepare_theme_snapshot_cache(catalog: ArtifactCatalog, trade_date: str,
                                  previous: str, asof: str,
                                  diagnostics: list[str] | None = None) -> bool:
    """Build the target-day theme view from PIT-ready Core artifacts only.

    On False, ``diagnostics`` (when given) receives one specific reason per missing input (C587)."""
    diag = diagnostics if diagnostics is not None else []
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
    if not pool:
        diag.append("limit_up_pool:" + _why_unavailable(catalog, "limit_up_pool", previous, asof))
    if not registry:
        diag.append(f"plate_registry:no_version_on_or_before_{trade_date}_available_at_or_before_asof")
    if not reasons:
        diag.append("limit_reasons:no_pit_ready_rows_on_or_before_" + trade_date + ";latest=" +
                    _why_unavailable(catalog, "limit_reasons", previous, asof))
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
        diag.append("trade_calendar:previous_session_unavailable")
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
    if not members_data:
        # Not fatal (the snapshot degrades to pool/reason labels) but never silent.
        diag.append("plate_members:" + _why_unavailable(catalog, "plate_members", previous, asof) + "(degraded_input)")
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
        diag.append("theme_snapshot:node_status=" + str(snapshot.get("status")))
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


THEME_FETCH_JOBS = {"limit_up_pool": "limit_up_pool", "limit_reasons": "limit_reasons",
                    "plate_members": "plate_members"}


def _theme_gap_jobs(diag: list[str]) -> list[str]:
    return [job for prefix, job in THEME_FETCH_JOBS.items() if any(d.startswith(prefix + ":") for d in diag)]


def build_prep_report(root: str | Path, trade_date: str, *, write: bool = False,
                      asof: str | None = None, event_id: str = "manual", theme_wait_seconds: float = 0.0,
                      theme_poll_seconds: float = 30.0, theme_on_missing: str = "fail",
                      sleep: Any = time.sleep) -> dict[str, Any]:
    """``theme_wait_seconds``/``theme_poll_seconds``/``theme_on_missing`` (C587, from schedule.json ``args.theme_deps``):
    when the yesterday pool / reasons / members the theme snapshot needs are missing or not yet available, retry the
    fetch and re-check inside the wait window instead of failing at once.  After the window ``fail`` keeps the old
    error, ``degrade`` reports a single warning (status PARTIAL) and still returns the other preparation steps."""
    if theme_on_missing not in {"fail", "degrade"}:
        raise ValueError("invalid_theme_on_missing")
    date.fromisoformat(trade_date)
    live_default_asof = asof is None
    asof = asof or datetime.now().astimezone().isoformat()
    if _pit_time(asof) is None:
        raise ValueError("invalid_preparation_asof")
    root = Path(root).expanduser()
    catalog = ArtifactCatalog(root)
    previous = previous_trade_date(root, trade_date)
    repo = Path(__file__).resolve().parents[2]
    records: dict[str, dict[str, Any]] = {}
    report_key = f"core:prep_report:{trade_date}:{event_id}:{uuid.uuid4().hex}"
    url = os.getenv("CORE_REDIS_URL") or os.getenv("REDIS_URL")
    client = None
    if url:
        try:
            import redis as redis_package
            client = redis_package.Redis.from_url(url, decode_responses=True,
                                                   socket_connect_timeout=0.2, socket_timeout=0.2)
        except Exception as exc:
            logging.getLogger(__name__).warning("prep report Redis unavailable: %s", type(exc).__name__)

    def publish(current=None, phase=None):
        nonlocal client
        if client is not None:
            try:
                client.set(report_key, json.dumps({"trade_date": trade_date, "event_id": event_id,
                    "current_step": current, "phase": phase, "step_timings": list(records.values()),
                    "timing_summary": {"check_ms_total": round(sum(r["check_ms"] for r in records.values()), 3),
                        "fill_ms_total": round(sum(r["fill_ms"] for r in records.values()), 3),
                        "steps_filled": sum(r["filled"] for r in records.values())}},
                    ensure_ascii=False, separators=(",", ":")), ex=14 * 86400)
            except Exception as exc:
                logging.getLogger(__name__).warning("prep report Redis write failed: %s", type(exc).__name__)
                client = None

    def begin(name, dataset, day):
        record = records.setdefault(name, {"step": name, "dataset": dataset, "trade_date": day,
            "status": "RUNNING", "check_ms": 0.0, "filled": False, "fill_script": None,
            "fill_exit": None, "fill_ms": 0.0})
        publish(name, "running")
        return record

    original_step, original_latest, original_run = globals()["_step"], globals()["_latest_step"], globals()["_run"]
    def checked_step(name, dataset, day, *args, **kwargs):
        record = begin(name, dataset, day)
        started = time.monotonic()
        item = original_step(name, dataset, day, *args, **kwargs)
        record.update(status=item["status"], trade_date=item["trade_date"])
        record["check_ms"] += round((time.monotonic() - started) * 1000, 3)
        publish(name, "completed")
        return item
    def checked_latest(name, dataset, day, *args, **kwargs):
        record = begin(name, dataset, day)
        started = time.monotonic()
        item = original_latest(name, dataset, day, *args, **kwargs)
        record.update(status=item["status"], trade_date=item["trade_date"])
        record["check_ms"] += round((time.monotonic() - started) * 1000, 3)
        publish(name, "completed")
        return item
    def tracked_run(repo, script, args):
        job = args[args.index("--job") + 1] if "--job" in args else None
        name = {"symbol_universe": "symbol_universe", "daily_kline": "previous_daily_kline",
                "limit_up_pool": "yesterday_limit_pool", "limit_reasons": "limit_reasons",
                "hot_plates": "hot_plates", "plate_members": "plate_members"}.get(job)
        name = name or {"capture_ths_hot_rank.py": "hot_rank", "build_auction_baseline.py": "auction_baseline",
                        "build_plate_amount_baseline.py": "plate_amount_baseline"}.get(script, "theme_snapshot")
        record = begin(name, name, trade_date)
        record.update(filled=True, fill_script=script, fill_exit=None)
        publish(name, "filling")
        started = time.monotonic()
        try:
            rc, out = original_run(repo, script, args)
            record["fill_exit"] = rc
            for line in out.splitlines():
                try:
                    payload = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(payload, dict) and isinstance(payload.get("reused"), bool):
                    record["reused_prior_version"] = payload["reused"]
                    if payload["reused"]:
                        record["filled"] = False
            return rc, out
        finally:
            record["fill_ms"] += round((time.monotonic() - started) * 1000, 3)
            publish(name, "completed")
    _step, _latest_step, _run = checked_step, checked_latest, tracked_run
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
    skipped = steps[-1]
    record = begin(skipped["step"], skipped["dataset"], skipped["trade_date"])
    record["status"] = skipped["status"]
    publish(skipped["step"], "completed")
    errors: list[str] = []
    error_codes: list[str | None] = []   # the registered code each failed job script declared (None: none declared)
    error_steps: list[dict[str, str | None]] = []
    warnings: list[str] = []
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
                logging.getLogger(__name__).error("symbol_universe preparation failed: %s", out[-2000:])
                errors.append("symbol_universe:COMMAND_FAILED")
                error_codes.append(_declared_code(out))
                error_steps.append({"step": errors[-1].split(":", 1)[0], "code": error_codes[-1]})
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
                error_codes.append(_declared_code(out))
                error_steps.append({"step": errors[-1].split(":", 1)[0], "code": error_codes[-1]})
        if _manifest(catalog, "daily_kline", previous, asof=asof) and (
                not _manifest(catalog, "daily_indicators", previous, asof=asof)
                or not _manifest(catalog, "daily_report_features", previous, asof=asof)):
            feature_names = [name for name, dataset in (("daily_indicators", "daily_indicators"),
                ("daily_report_features", "daily_report_features")) if not _manifest(catalog, dataset, previous, asof=asof)]
            for name in feature_names:
                record = begin(name, name, previous)
                record.update(filled=True, fill_script="daily_history.py", fill_exit=None)
            publish(",".join(feature_names), "filling")
            feature_exit = 0
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
                    from .daily_history import pit_asof_with_late_inputs
                    materialize_daily_report_features(catalog, trade_date=previous,
                        asof=pit_asof_with_late_inputs(catalog, previous), redis=redis)
            except Exception as exc:
                feature_exit = 1
                errors.append("daily_features:" + str(exc)[:240])
                error_steps.append({"step": "daily_features", "code": None})
            elapsed = round((time.monotonic() - started) * 1000, 3)
            for index, name in enumerate(feature_names):
                records[name].update(fill_exit=feature_exit, fill_ms=elapsed if index == 0 else 0.0)
            publish(",".join(feature_names), "completed")
            timings["daily_indicators"] = elapsed
            timings["daily_report_features"] = elapsed
        for job, dataset in (("limit_up_pool", "limit_up_pool"),
                             ("limit_reasons", "limit_reasons"),
                             ("hot_plates", "hot_plates"),
                             ("plate_members", "plate_members")):
            if not (_plate_members_snapshot(asof=asof) if dataset == "plate_members" else
                    _manifest(catalog, dataset, previous, asof=asof)):
                started = time.monotonic()
                extra = ["--max-requests", "15"]
                rc, out = _run(repo, "run_data_jobs.py", ["--job", job, "--date", previous,
                    "--write", "--artifact-root", str(root), *extra])
                timings[{"limit_up_pool": "yesterday_limit_pool", "limit_reasons": "limit_reasons",
                         "hot_plates": "hot_plates", "plate_members": "plate_members"}[dataset]] = round(
                             (time.monotonic() - started) * 1000, 3)
                if rc:
                    errors.append(job + ":COMMAND_FAILED")
                    error_codes.append(_declared_code(out))
                    error_steps.append({"step": job, "code": error_codes[-1]})
        if not _latest_step("hot_rank", "hot_rank", trade_date, catalog, asof=asof)["trade_date"]:
            started = time.monotonic()
            rc, out = _run(repo, "capture_ths_hot_rank.py", ["--execute", "--artifact-root", str(root)])
            timings["hot_rank"] = round((time.monotonic() - started) * 1000, 3)
            if rc:
                errors.append("hot_rank:COMMAND_FAILED")
                error_codes.append(_declared_code(out))
                error_steps.append({"step": "hot_rank", "code": error_codes[-1]})
        started = time.monotonic()
        rc, out = _run(repo, "build_auction_baseline.py", ["--date", trade_date, "--write",
            "--artifact-root", str(root)])
        timings["auction_baseline"] = round((time.monotonic() - started) * 1000, 3)
        if rc:
            errors.append("auction_baseline:COMMAND_FAILED:" + out[-240:])
            error_codes.append(_declared_code(out))
            error_steps.append({"step": "auction_baseline", "code": error_codes[-1]})
        started = time.monotonic()
        rc, out = _run(repo, "build_plate_amount_baseline.py", ["--date", trade_date, "--write",
            "--artifact-root", str(root)])
        timings["plate_amount_baseline"] = round((time.monotonic() - started) * 1000, 3)
        if rc:
            errors.append("plate_amount_baseline:COMMAND_FAILED:" + out[-240:])
            error_codes.append(_declared_code(out))
            error_steps.append({"step": "plate_amount_baseline", "code": error_codes[-1]})
        try:
            theme_record = begin("theme_snapshot", "theme_snapshot", trade_date)
            theme_record.update(filled=True, fill_script="_prepare_theme_snapshot_cache", fill_exit=None)
            publish("theme_snapshot", "filling")
            theme_started = time.monotonic()
            budget = max(0.0, float(theme_wait_seconds))
            waited = 0.0
            polls = 0
            while True:
                if live_default_asof:
                    # Jobs fetched above are visible only from their commit time; a live run's cut-off is "now".
                    asof = datetime.now().astimezone().isoformat()
                diag: list[str] = []
                built = _prepare_theme_snapshot_cache(fresh_catalog := ArtifactCatalog(root),
                                                      trade_date, previous, asof, diagnostics=diag)
                if built and not diag:
                    break
                if waited >= budget:
                    break
                polls += 1
                nap = max(0.0, min(float(theme_poll_seconds), budget - waited))
                sleep(nap)
                retry_started = time.monotonic()
                for job in _theme_gap_jobs(diag):
                    rc, _out = _run(repo, "run_data_jobs.py", ["--job", job, "--date", previous, "--write",
                                    "--artifact-root", str(root), "--max-requests", "15"])
                waited += max(nap, 0.001) + (time.monotonic() - retry_started)
            timings["theme_snapshot"] = round((time.monotonic() - theme_started) * 1000, 3)
            theme_record.update(fill_ms=timings["theme_snapshot"], fill_exit=0 if built else 1, status="READY" if built and not diag else "PARTIAL")
            publish("theme_snapshot", "completed")
            log = logging.getLogger(__name__)
            if built and not diag:
                pass
            elif built:
                warnings.append("theme_snapshot:DEGRADED_INPUT:" + ";".join(diag))
                log.warning("theme_snapshot built with missing inputs after %s polls: %s", polls, "; ".join(diag))
            elif theme_on_missing == "degrade":
                warnings.append("theme_snapshot:SKIPPED_MISSING_INPUT:" + ";".join(diag))
                log.warning("theme_snapshot skipped (degrade) after %s polls: %s", polls, "; ".join(diag))
            else:
                log.error("theme_snapshot PRECOMPUTE_UNAVAILABLE after %s polls: %s", polls, "; ".join(diag))
                errors.append("theme_snapshot:PRECOMPUTE_UNAVAILABLE:" + ";".join(diag)[:600])
                error_steps.append({"step": "theme_snapshot", "code": None})
        except Exception as exc:
            theme_record.update(status="FAILED", fill_exit=1, fill_ms=round((time.monotonic() - theme_started) * 1000, 3))
            publish("theme_snapshot", "completed")
            errors.append("theme_snapshot:" + str(exc)[:200])
            error_steps.append({"step": "theme_snapshot", "code": None})
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
            records[item["step"]]["status"] = item["status"]
            if item["step"] in timings:
                item["elapsed_ms"] = timings[item["step"]]
    step_timings = list(records.values())
    summary = {"check_ms_total": round(sum(r["check_ms"] for r in step_timings), 3),
               "fill_ms_total": round(sum(r["fill_ms"] for r in step_timings), 3),
               "steps_filled": sum(r["filled"] for r in step_timings)}
    publish(None, "completed")
    return {"step_timings": step_timings, "timing_summary": summary, "report_key": report_key,
            "trade_date": trade_date, "previous_trade_date": previous,
            "mode": "WRITE" if write else "DRY_RUN", "steps": steps,
            "errors": errors, "error_steps": error_steps, "error_codes": [code for code in error_codes if code], "warnings": warnings,
            "status": "READY" if all(s["status"] == "READY" for s in steps) and not errors and not warnings else "PARTIAL"}
