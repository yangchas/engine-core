"""Read immutable catalog versions, optionally resolving Core Redis pointers first."""
from __future__ import annotations

import json
import gzip
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .registry import get_dataset
from .storage import ArtifactCatalog


@dataclass(frozen=True)
class DatasetRead:
    rows: list[dict[str, Any]]
    manifest: dict[str, Any]
    discovered_by: str
    status: str


def _checked_pool_value(func, *args):
    from .cold_archive import CorruptArchive
    try:
        return func(*args)
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise CorruptArchive(str(exc) or type(exc).__name__) from exc


def _read_today_pool(trade_date: str | None, client: Any = None, cold_store: Any = None) -> DatasetRead:
    """Read a point snapshot from Redis or its verified cold copy, never catalog."""
    import hashlib
    from . import cold_archive
    from .redis_day_view import hot_view_expire_at

    name = "limit_up_pool_today"
    owned_client = client is None
    store = None
    try:
        if client is None:
            from .limit_pool_hot_view import _client
            client = _client()
        key = f"core:cache:data:{name}:{trade_date or 'latest'}"
        pointer_raw = client.get(key)
        pointer = _checked_pool_value(json.loads, pointer_raw) if pointer_raw is not None else None
        if pointer is not None:
            day, version = _checked_pool_value(lambda p: (p['trade_date'], p['version']), pointer)
            if trade_date and day != trade_date:
                raise cold_archive.CorruptArchive('pointer_trade_date_mismatch')
            raw = client.get(f"core:cache:data:{name}:{day}:{version}")
            if raw is not None:
                value = _checked_pool_value(_validate_today_view, raw, day, version)
                return DatasetRead(value['rows'], value, 'redis', 'READY')
        if not trade_date:
            return DatasetRead([], {}, 'none', 'UNKNOWN')
        if cold_store is None:
            if not cold_archive._enabled():
                raise RuntimeError('cold_archive_disabled')
            store = cold_archive.TdStore()
        else:
            store = cold_store
        archive_version = store.latest_version(name, trade_date)
        if archive_version is None:
            return DatasetRead([], {}, 'none', 'MISSING')
        raw = cold_archive.read_envelope(name, trade_date, archive_version, store=store)
        value = _checked_pool_value(_validate_today_view, raw, trade_date, None)
        version = value['version']
        if archive_version not in (version, version + ':' + hashlib.sha256(raw).hexdigest()[:16]):
            raise cold_archive.CorruptArchive('cold_archive_version_mismatch')
        deadline = hot_view_expire_at(trade_date)
        version_key = f"core:cache:data:{name}:{trade_date}:{version}"
        if not client.set(version_key, raw, exat=deadline, nx=True):
            existing = client.get(version_key)
            existing_bytes = existing.encode('utf-8') if isinstance(existing, str) else existing
            if existing_bytes != raw:
                raise cold_archive.CorruptArchive('redis_version_conflict')
        restored_pointer = json.dumps({'trade_date': trade_date, 'version': version,
                                       'as_of': value.get('as_of')})
        if not client.set(key, restored_pointer, exat=deadline, nx=True):
            current = _checked_pool_value(json.loads, client.get(key))
            current_day, current_version = _checked_pool_value(lambda p: (p['trade_date'], p['version']), current)
            current_raw = client.get(f"core:cache:data:{name}:{current_day}:{current_version}")
            if current_raw is None:
                raise cold_archive.CorruptArchive('current_version_missing')
            value = _checked_pool_value(_validate_today_view, current_raw, trade_date, current_version)
            return DatasetRead(value['rows'], value, 'redis', 'READY')
        return DatasetRead(value['rows'], value, 'td', 'READY')
    except cold_archive.CorruptArchive as exc:
        import logging
        reason = str(exc) or type(exc).__name__
        logging.getLogger(__name__).warning('presence_corrupt dataset=%s trade_date=%s reason=%s', name, trade_date, reason)
        return DatasetRead([], {'reason': reason}, 'none', 'CORRUPT')
    except Exception:
        return DatasetRead([], {}, 'none', 'UNKNOWN')
    finally:
        if store is not None and cold_store is None:
            try:
                store.close()
            except Exception:
                pass
        if owned_client and client is not None:
            try:
                client.close()
            except Exception:
                pass


def _validate_today_view(raw: Any, day: str, version: str | None) -> dict[str, Any]:
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get('dataset') != 'limit_up_pool_today'
            or value.get('trade_date') != day or not value.get('version')
            or (version is not None and value['version'] != version)
            or not isinstance(value.get('rows'), list)
            or value.get('row_count') != len(value['rows'])):
        raise ValueError('invalid_today_pool_view')
    return value


def load_dataset(name: str, trade_date: str | None = None, *, prefer: str = "redis",
                 redis: Any = None, catalog: ArtifactCatalog | None = None,
                 artifact_root: str | Path | None = None, now: str | None = None) -> DatasetRead | None:
    if prefer not in {"redis", "file"}: raise ValueError("prefer must be redis or file")
    spec = get_dataset(name)
    if name == "limit_up_pool":
        from .limit_pool_hot_view import read
        if trade_date is None:
            return DatasetRead([], {}, 'none', 'MISSING')
        rows, meta = read(trade_date, now or datetime.now(timezone.utc).isoformat(), redis_client=redis)
        return DatasetRead(rows, meta, meta.get('storage_source', 'none'), meta['status'])
    if name == "limit_up_pool_today":
        return _read_today_pool(trade_date, redis)
    if name == "hot_plates_gaps":
        if redis is None or trade_date is None:
            return None
        rows = []
        for key in redis.scan_iter(match=f"core:cache:data:hot_plates_gaps:{trade_date}:*", count=100):
            raw = redis.get(key)
            if raw:
                row = json.loads(raw)
                if isinstance(row, dict) and row.get("trade_date") == trade_date:
                    rows.append(row)
        return DatasetRead(rows, {"dataset": name, "trade_date": trade_date}, "redis", "READY") if rows else None
    selected: dict[str, Any] | None = None
    discovered = "catalog"
    if name in {"plate_members", "plate_registry", "stock_static"} and os.environ.get("TD_CONFIG"):
        from .reference_snapshot_td import ReferenceSnapshotStore

        with ReferenceSnapshotStore() as store:
            latest = store.read_latest(name)
        if latest is not None and (trade_date is None or latest.get("trade_date") == trade_date):
            selected = {key: value for key, value in latest.items() if key != "rows"}
            selected.update({"dataset": name, "trade_date": latest.get("trade_date"),
                             "business_status": "FORMAL", "rows": latest["rows"]})
            discovered = "td"
    if selected is None and prefer == "redis" and redis is not None:
        key = (f"core:cache:data:{name}:{trade_date}" if trade_date else
               f"core:cache:data:{name}:latest")
        raw = redis.get(key)
        if raw is not None:
            try: pointer = json.loads(raw)
            except (TypeError, ValueError): pointer = {}
            if isinstance(pointer, dict):
                manifest_uri = pointer.get("manifest")
                version_key = (f"core:cache:data:{name}:{pointer.get('trade_date')}:{pointer.get('version')}"
                               if pointer.get("trade_date") and pointer.get("version") else None)
                version_raw = redis.get(version_key) if version_key else None
                if version_raw:
                    try: selected = json.loads(version_raw)
                    except (TypeError, ValueError): selected = None
                    discovered = "redis"
                if selected is None and manifest_uri and Path(manifest_uri).is_file():
                    selected = json.loads(Path(manifest_uri).read_text(encoding="utf-8"))
                    discovered = "redis"
    catalog = catalog or (ArtifactCatalog(artifact_root) if artifact_root is not None else None)
    if selected is None and catalog is not None:
        path = catalog.locate(name, trade_date)
        if path is None: return None
        selected = json.loads(path.read_text(encoding="utf-8"))
    if selected is None: return None
    if trade_date and selected.get("trade_date") != trade_date: return None
    rows = selected.get("rows")
    if rows is None:
        rows_uri = selected.get("rows_uri")
        if not rows_uri:
            if catalog is None and selected.get("manifest_uri"):
                manifest_path = Path(selected["manifest_uri"])
                selected = json.loads(manifest_path.read_text(encoding="utf-8"))
                catalog = ArtifactCatalog(manifest_path.parents[3])
            if catalog is None: return None
            rows = catalog.read_rows(Path(selected["manifest_uri"]))
        else:
            rows = []
            rows_path = Path(rows_uri)
            opener = gzip.open if rows_path.suffix == ".gz" else open
            with opener(rows_path, "rt", encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle if line.strip()]
    freshness = selected.get("available_at")
    stale = False
    # TTL describes whether a Redis hot view should be refreshed. An explicit
    # historical catalog read is immutable and does not become stale merely
    # because its trade date is old.
    if freshness and trade_date is None:
        try:
            current = datetime.fromisoformat((now or datetime.now(timezone.utc).isoformat()).replace("Z", "+00:00"))
            fetched = datetime.fromisoformat(freshness.replace("Z", "+00:00"))
            stale = (current - fetched).total_seconds() > spec.redis_ttl_seconds
        except (TypeError, ValueError): stale = True
    return DatasetRead(rows, selected, discovered, "DEGRADED" if stale else "READY")
