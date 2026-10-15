"""Read immutable catalog versions, optionally resolving Core Redis pointers first."""
from __future__ import annotations

import json
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


def load_dataset(name: str, trade_date: str | None = None, *, prefer: str = "redis",
                 redis: Any = None, catalog: ArtifactCatalog | None = None,
                 artifact_root: str | Path | None = None, now: str | None = None) -> DatasetRead | None:
    if prefer not in {"redis", "file"}: raise ValueError("prefer must be redis or file")
    spec = get_dataset(name)
    selected: dict[str, Any] | None = None
    discovered = "catalog"
    if prefer == "redis" and redis is not None:
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
            with Path(rows_uri).open(encoding="utf-8") as handle:
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
