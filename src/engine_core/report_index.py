"""Versioned, queryable records for completed daily reports."""
from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any

from .data_pipeline.storage import ArtifactCatalog


def build_record(manifest: Mapping[str, Any], *, out_dir: str | Path,
                 completed_at: str) -> dict[str, Any]:
    slices = manifest.get("slices")
    if not isinstance(slices, Mapping):
        slices = {}
    input_versions = {}
    for name, value in slices.items():
        item = value if isinstance(value, Mapping) else {}
        input_versions[str(name)] = {
            "version": item.get("version") or "UNKNOWN",
            "available_at": item.get("available_at") or "UNKNOWN",
            "status": item.get("status") or "UNKNOWN",
            "reason": item.get("reason") or "UNKNOWN",
            "used_date": item.get("used_date") or "UNKNOWN",
        }
    return {
        "date": manifest.get("trade_date") or "UNKNOWN",
        "stage": manifest.get("stage") or "UNKNOWN",
        "asof": manifest.get("asof") or "UNKNOWN",
        "checkpoint": manifest.get("checkpoint") or "UNKNOWN",
        "completed_at": completed_at,
        "out_dir": str(out_dir),
        "degraded": sorted(manifest.get("degraded") or []),
        "input_versions": input_versions,
        "report_json_bytes": manifest.get("report_json_bytes", "UNKNOWN"),
    }


def _records_for_date(catalog: ArtifactCatalog, date: str) -> list[dict[str, Any]]:
    entry = catalog.read().get("datasets", {}).get("report_index", {})
    versions = entry.get("dates", {}).get(date, [])
    records = []
    for version in versions:
        try:
            records.extend(catalog.read_rows(Path(version["manifest"])))
        except (KeyError, OSError, ValueError, TypeError):
            continue
    return records


def _stored_record_result(catalog: ArtifactCatalog, date: str,
                          record: Mapping[str, Any]) -> dict[str, Any] | None:
    entry = catalog.read().get("datasets", {}).get("report_index", {})
    for version in entry.get("dates", {}).get(date, []):
        try:
            manifest_path = Path(version["manifest"])
            if any(row == dict(record) for row in catalog.read_rows(manifest_path)):
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                return {**dict(record), **manifest}
        except (KeyError, OSError, ValueError, TypeError):
            continue
    return None


def append_record(catalog_root: str | Path, record: Mapping[str, Any]) -> dict[str, Any]:
    """Append one report record, assigning a stable revision within its identity."""
    catalog = ArtifactCatalog(catalog_root)
    date = str(record.get("date") or "UNKNOWN")
    stage = str(record.get("stage") or "UNKNOWN")
    checkpoint = str(record.get("checkpoint") or "UNKNOWN")
    current = _records_for_date(catalog, date)
    for existing in current:
        if (existing.get("stage") == stage and existing.get("checkpoint", "UNKNOWN") == checkpoint
                and {k: v for k, v in existing.items() if k != "revision"} == dict(record)):
            stored = _stored_record_result(catalog, date, existing)
            if stored is not None:
                return stored
            return existing
    revision = 1 + sum(1 for existing in current
                       if existing.get("stage") == stage
                       and existing.get("checkpoint", "UNKNOWN") == checkpoint)
    versioned = {**dict(record), "revision": revision}
    return catalog.commit("report_index", date, [versioned], versioned,
                          source="report", authority="LIVE") | {"revision": revision}


def list_records(catalog_root: str | Path, date: str,
                 stage: str | None = None) -> list[dict[str, Any]]:
    catalog = ArtifactCatalog(catalog_root)
    records = _records_for_date(catalog, date)
    if stage is not None:
        records = [record for record in records if record.get("stage") == stage]
    return sorted(records, key=lambda record: str(record.get("completed_at") or ""))
