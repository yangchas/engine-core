"""Guardian adapter for latest-only TD plate/stock reference snapshots.

The builders are pure (`engine_core.data_pipeline.static_data`); this module
owns source reads, freshness decisions, TD version switches, and readback.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time as time_module
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Mapping

from zoneinfo import ZoneInfo

from engine_core.errors import CoreError
from engine_core.data_pipeline.static_data import (
    build_plate_members,
    build_plate_registry,
    build_stock_static,
    static_refresh_decision,
)
from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.data_pipeline.reference_snapshot_td import ReferenceSnapshotStore

SHANGHAI = ZoneInfo("Asia/Shanghai")


class KaipanPlateMembersIncomplete(RuntimeError):
    """A whole reference snapshot can be retried when any plate fetch failed."""

    error_code = "KAIPAN_PLATE_MEMBERS_INCOMPLETE"


def _plate_members_query_date(core_root: Path, event_day: str) -> str:
    """Kaipan serves historical plate membership, so refresh the last completed trading day."""
    from engine_core.prep_trade_day import previous_trade_date

    query_day = previous_trade_date(core_root, event_day)
    if not query_day:
        raise RuntimeError("plate_members_previous_trade_date_unavailable")
    return query_day


def _snapshot(catalog: ArtifactCatalog, dataset: str):
    path = catalog.locate(dataset)
    if path is None or not path.is_file():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return path, manifest, catalog.read_rows(path)


def _prior_universe(catalog: ArtifactCatalog, latest_day: str):
    dates = (catalog.read().get("datasets", {}).get("symbol_universe", {}).get("dates") or {})
    prior = sorted(day for day in dates if day < latest_day)
    if not prior:
        return None
    path = catalog.locate("symbol_universe", prior[-1])
    if path is None:
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return path, manifest, catalog.read_rows(path)


def _symbols(rows) -> set[str]:
    result = set()
    for row in rows:
        value = str(row.get("symbol") or row.get("code") or "").strip()
        value = value.rsplit(".", 1)[-1]
        if re.fullmatch(r"\d{6}", value):
            result.add(value)
    return result


def _timestamp(value: Any):
    if not isinstance(value, str) or not value:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return datetime.combine(date.fromisoformat(value), time.min, SHANGHAI)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed


def _last_available(snapshot):
    if snapshot is None:
        return None
    if isinstance(snapshot, Mapping):
        return _timestamp(snapshot.get("available_at"))
    _path, manifest, _rows = snapshot
    return _timestamp(manifest.get("available_at") or manifest.get("as_of") or manifest.get("trade_date"))


def _basic_payload():
    import baostock as bs

    login = bs.login()
    if getattr(login, "error_code", None) != "0":
        raise RuntimeError("baostock_login_failed")
    try:
        result = bs.query_stock_basic()
        if getattr(result, "error_code", None) != "0":
            raise RuntimeError("baostock_query_stock_basic_failed")
        rows = []
        while result.next():
            rows.append(result.get_row_data())
        return {"fields": list(getattr(result, "fields", []) or []), "rows": rows}
    finally:
        bs.logout()


class _LazyKaipanClient:
    def get_his_plate_ids(self, plate: str, day: str, index: int, size: int):
        import importlib
        api = importlib.import_module("pykaipan.pykaipan")
        return api.getHisPlateIds(p=plate, date=day, index=str(index), size=str(size))


def _registry_source(cfg: Mapping[str, Any]):
    source_path = cfg.get("registry_source")
    if not source_path:
        return None
    path = Path(str(source_path)).expanduser()
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    rows = payload.get("rows", payload.get("plates")) if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("plate_registry_source_rows_invalid")
    if not bool(cfg.get("registry_source_complete")):
        raise RuntimeError("plate_registry_source_completeness_not_declared")
    source_date = payload.get("source_date") if isinstance(payload, dict) else None
    return rows, {
        "configured_path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "source_date_echo": source_date,
        "completeness_declared": True,
    }


def refresh_static_data(event, now: datetime, guardian_root: Path | None = None):
    """Run configured static dataset refreshes for one Guardian event."""
    args = dict(getattr(event, "args", None) or {})
    cfg = args.get("static_data_refresh")
    if not isinstance(cfg, Mapping):
        raise ValueError("static_data_refresh_config_required")
    core_root = Path(os.getenv("CORE_DATA_ROOT", "~/core-data")).expanduser()
    catalog = ArtifactCatalog(core_root)
    universe = _snapshot(catalog, "symbol_universe")
    if universe is None:
        raise CoreError("UPSTREAM_NOT_READY", "symbol_universe_artifact_required")
    _universe_path, universe_manifest, universe_rows = universe
    universe_day = str(universe_manifest.get("trade_date") or "UNKNOWN")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", universe_day):
        raise RuntimeError("symbol_universe_trade_date_missing")
    symbols = _symbols(universe_rows)
    if not symbols:
        raise RuntimeError("symbol_universe_symbols_missing")
    prior_universe = _prior_universe(catalog, universe_day)
    prior_symbols = _symbols(prior_universe[2]) if prior_universe else set()
    new_universe_symbols = symbols - prior_symbols
    target_day = now.astimezone(SHANGHAI).date().isoformat()
    as_of = now.astimezone(SHANGHAI).isoformat()
    datasets = tuple(cfg.get("datasets") or ("plate_registry", "plate_members", "stock_static"))
    outputs: list[str] = []
    force = bool(cfg.get("force", False))

    with ReferenceSnapshotStore() as td:
        for dataset in datasets:
            if dataset not in {"plate_registry", "plate_members", "stock_static"}:
                raise ValueError("static_dataset_not_supported:" + str(dataset))
            latest = td.read_latest(dataset)
            snapshot_symbols = (_symbols(latest["rows"]) if latest and dataset == "stock_static"
                                else prior_symbols)
            decision = static_refresh_decision(
                now=now,
                trigger_time=str(cfg.get("trigger_time", "08:50")),
                last_available_at=_last_available(latest),
                max_age_days=int(cfg.get("max_age_days", 8)),
                weekly_days=int(cfg.get("weekly_days", 7)),
                universe_symbols=(symbols if dataset == "stock_static" else new_universe_symbols),
                snapshot_symbols=snapshot_symbols,
            )
            if not force and not decision["refresh"]:
                stale = (f";STALE_ALERT age_days={decision['age_days']}" if decision["stale_alert"] else "")
                outputs.append(f"SKIP:{dataset} fresh{stale}")
                continue

            source_dates: dict[str, Any] = {}
            source: str
            started = time_module.monotonic()
            if dataset == "plate_registry":
                configured = _registry_source(cfg)
                if configured is None:
                    age = decision["age_days"]
                    suffix = f";STALE_ALERT age_days={age}" if decision["stale_alert"] else ""
                    outputs.append("SOURCE_NOT_CONFIGURED:plate_registry" + suffix)
                    continue
                source_rows, evidence = configured
                source_dates["registry_file"] = evidence
                source = "configured_registry_file"
                partition = build_plate_registry(source_rows, trade_date=target_day, as_of=as_of,
                                                 source_dates=source_dates, provider=source)
                version = td.begin(dataset, source=source, available_at=as_of)
                committed = td.write_and_activate(
                    dataset, version, partition.rows, source=source, available_at=as_of,
                    request_count=0, elapsed_ms=max(1, int((time_module.monotonic() - started) * 1000)),
                )
            elif dataset == "plate_members":
                members_trade_date = _plate_members_query_date(core_root, target_day)
                registry_snapshot = td.read_latest("plate_registry")
                registry_manifest: Mapping[str, Any]
                if registry_snapshot is not None:
                    registry_rows = registry_snapshot["rows"]
                    registry_manifest = registry_snapshot
                else:
                    registry_snapshot = _snapshot(catalog, "plate_registry")
                    if registry_snapshot is None:
                        raise RuntimeError("plate_registry_reference_missing")
                    _registry_path, registry_manifest, registry_rows = registry_snapshot
                plate_codes = [str(row.get("plate_code") or row.get("code") or "").strip()
                               for row in registry_rows]
                plate_codes = list(dict.fromkeys(code for code in plate_codes if code))
                if not plate_codes:
                    raise RuntimeError("plate_registry_codes_missing")
                plate_classes = {str(row.get("plate_code") or row.get("code")): str(row.get("class") or "UNKNOWN")
                                 for row in registry_rows}
                from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider
                source = "kaipan_getHisPlateIds"
                version = td.begin(dataset, source=source, available_at=as_of)
                provider = KaipanProvider(
                    _LazyKaipanClient(), plate_codes=plate_codes, plate_classes=plate_classes,
                    max_requests=max(int(cfg.get("kaipan_max_requests", 400)), len(plate_codes) * 8),
                    min_interval_seconds=0.2,
                )
                fetch_started = time_module.monotonic()
                fetched = provider.plate_members(members_trade_date, concurrency=4)
                if fetched.get("partial"):
                    failed_plates = fetched.get("failed_plates") or []
                    failure_types = Counter(str(row.get("error_type") or "UNKNOWN")
                                            for row in failed_plates if isinstance(row, Mapping))
                    failed_codes = [str(row.get("plate_code") or "") for row in failed_plates
                                    if isinstance(row, Mapping) and row.get("plate_code")]
                    elapsed_ms = max(0, int((time_module.monotonic() - fetch_started) * 1000))
                    raise KaipanPlateMembersIncomplete(
                        "kaipan_plate_members_incomplete_retry_all:"
                        f"failed={len(failed_plates)},completed={fetched.get('completed_plate_count', 0)},"
                        f"queried={fetched.get('queried_plate_count', 0)},"
                        f"unqueried={fetched.get('unqueried_plate_count', 0)},"
                        f"query_trade_date={members_trade_date},requests={fetched.get('request_count', 0)},"
                        f"elapsed_ms={elapsed_ms},"
                        f"failure_types={json.dumps(dict(sorted(failure_types.items())), separators=(',', ':'))},"
                        f"failed_plate_codes={','.join(failed_codes[:20])}"
                    )
                source_dates["plate_registry"] = {
                    "trade_date": registry_manifest.get("trade_date"),
                    "version": registry_manifest.get("version"),
                    "available_at": registry_manifest.get("available_at"),
                }
                source_dates[source] = {"query_trade_date": members_trade_date,
                                        "request_count": fetched.get("request_count")}
                partition = build_plate_members(
                    fetched["rows"], registry_rows, trade_date=members_trade_date, as_of=as_of,
                    source_dates=source_dates,
                )
                fetched_row_count = len(fetched["rows"])
                if len(partition.rows) != fetched_row_count:
                    raise RuntimeError(
                        f"kaipan_plate_members_row_count_changed:{fetched_row_count}:{len(partition.rows)}"
                    )
                elapsed_ms = max(1, int((time_module.monotonic() - fetch_started) * 1000))
                committed = td.write_and_activate(
                    dataset, version, partition.rows, source=source, available_at=as_of,
                    request_count=int(fetched.get("request_count") or 0), elapsed_ms=elapsed_ms,
                )
                committed.update({key: fetched.get(key) for key in (
                    "queried_plate_count", "completed_plate_count", "failed_plates",
                    "unqueried_plate_count")})
                committed["fetched_row_count"] = fetched_row_count
            else:
                source = "baostock_stock_basic"
                source_dates["symbol_universe"] = {
                    "trade_date": universe_day,
                    "version": universe_manifest.get("version"),
                    "available_at": universe_manifest.get("available_at"),
                }
                source_dates[source] = {
                    "source_date_echo": None,
                    "snapshot_semantics": "current response; provider has no date echo",
                    "request_count": 1,
                }
                version = td.begin(dataset, source=source, available_at=as_of)
                partition = build_stock_static(
                    universe_rows, _basic_payload(), trade_date=target_day, as_of=as_of,
                    source_dates=source_dates,
                )
                committed = td.write_and_activate(
                    dataset, version, partition.rows, source=source, available_at=as_of,
                    request_count=1,
                    elapsed_ms=max(1, int((time_module.monotonic() - started) * 1000)),
                )
            outputs.append("REFRESHED:" + json.dumps(committed, sort_keys=True, separators=(",", ":")))

    status = "DEGRADED:" if any(value.startswith("SOURCE_NOT_CONFIGURED:") for value in outputs) else ""
    return status + ";".join(outputs)
