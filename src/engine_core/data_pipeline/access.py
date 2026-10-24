"""Read immutable catalog artifacts through one normalized data entry point."""
from __future__ import annotations

import gzip
import json
import math
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .registry import get_dataset
from .registry.canonical import canonicalize_row
from .registry.resolve import AdapterResolution, resolve_adapter
from .storage import ArtifactCatalog


@dataclass(frozen=True)
class DataSet:
    rows: list[dict[str, Any]]
    meta: dict[str, Any]


def _datetime(value: str | datetime | None) -> datetime | None:
    if value is None: return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_as_of") from exc
    if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _prepare_candidate(entry: Mapping[str, Any], manifest: Mapping[str, Any],
                       path: Path) -> dict[str, Any]:
    gate = manifest.get("gate")
    quality = manifest.get("quality")
    coverage = quality.get("coverage") if isinstance(quality, Mapping) else None
    if isinstance(coverage, bool) or not isinstance(coverage, (int, float)) or not math.isfinite(coverage):
        coverage = None
    gate_status = ((gate.get("gate_status") if isinstance(gate, Mapping) else None)
                   or entry.get("gate_status") or "UNKNOWN")
    business = manifest.get("business_status") or entry.get("business_status")
    return {
        "entry": entry, "manifest_data": manifest, "manifest_path": str(path),
        "version": entry.get("version") or manifest.get("version"),
        "source": manifest.get("source") or entry.get("source"),
        "gate_status": str(gate_status).upper(),
        "business_status": str(business).upper() if business is not None else None,
        "coverage": float(coverage) if coverage is not None else None,
        "available_at": manifest.get("available_at"), "_eliminated_reason": None,
    }


def select_version(candidates: Sequence[Mapping[str, Any]],
                   as_of: str | datetime | None = None) -> Mapping[str, Any] | None:
    """Choose the highest-quality candidate, independent of catalog append order."""
    cutoff = _datetime(as_of)
    eligible = []
    for candidate in candidates:
        try:
            available = _datetime(candidate.get("available_at"))
        except ValueError:
            available = None
        if cutoff is not None and (available is None or available > cutoff):
            continue
        eligible.append(candidate)
    if not eligible:
        return None

    def key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
        gate = candidate.get("gate_status", "UNKNOWN")
        gate_rank = 0 if gate in {"OK", "WARN"} else 2 if gate == "FAIL" else 1
        business = candidate.get("business_status")
        business_rank = 0 if business == "FORMAL" else 1 if business == "PROVISIONAL" else 2
        try:
            available = _datetime(candidate.get("available_at"))
        except ValueError:
            available = None
        available_rank = available.timestamp() if available is not None else float("-inf")
        coverage = candidate.get("coverage")
        coverage = (coverage if isinstance(coverage, (int, float)) and
                    not isinstance(coverage, bool) and math.isfinite(coverage) else -1)
        return (gate_rank, business_rank, -coverage, -available_rank,
                str(candidate.get("manifest_path", "")))

    return min(eligible, key=key)


def _candidate_info(candidate: Mapping[str, Any], reason: str | None) -> dict[str, Any]:
    return {
        "version": candidate.get("version"), "source": candidate.get("source"),
        "gate_status": candidate.get("gate_status"), "business_status": candidate.get("business_status"),
        "coverage": candidate.get("coverage"), "eliminated_reason": reason,
    }


def _read_rows(manifest: Mapping[str, Any], manifest_path: Path,
               root: Path) -> tuple[list[dict[str, Any]], int]:
    uri = manifest.get("rows_uri")
    if not isinstance(uri, str) or not uri:
        raise ValueError("rows_uri_missing")
    path = Path(uri).expanduser()
    if not path.is_absolute():
        options = (root / path, manifest_path.parent / path)
        path = next((item for item in options if item.is_file()), options[0])
    rows: list[dict[str, Any]] = []
    rejected = 0
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                rejected += 1
                continue
            if not isinstance(row, Mapping):
                rejected += 1
                continue
            rows.append(dict(row))
    return rows, rejected


def _empty_meta(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "MISSING", "version": None, "source": None, "adapter": None,
        "adapter_resolved_by": "unresolved", "business_status": None, "available_at": None,
        "gate_status": "UNKNOWN", "coverage": None, "candidates": candidates,
        "rows_total": 0, "rejected": 0, "flag_counts": {}, "unmapped_keys": {}, "reasons": ["no_readable_candidate"],
    }

def get(dataset: str, *, trade_date: str, tag: str | None = None,
        as_of: str | datetime | None = None, view: str = "canonical",
        root: str | Path | None = None,
        catalog: ArtifactCatalog | None = None) -> DataSet:
    """Read one catalog version from disk and optionally normalize registered fields."""
    if view not in {"raw", "canonical"}:
        raise ValueError("view must be raw or canonical")
    spec = get_dataset(dataset)
    base = root if root is not None else (catalog.root if catalog is not None else
                                          os.environ.get("CORE_DATA_ROOT", "~/core-data"))
    root_path = Path(base).expanduser()
    catalog = catalog or ArtifactCatalog(root_path)
    cutoff = _datetime(as_of)

    dataset_entry = catalog.read().get("datasets", {}).get(dataset, {})
    entries = dataset_entry.get("dates", {}).get(trade_date, [])
    if not isinstance(entries, list):
        entries = []
    loaded: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        manifest_ref = entry.get("manifest")
        path = Path(manifest_ref).expanduser() if isinstance(manifest_ref, str) else Path()
        if not path.is_absolute():
            path = root_path / path
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict): raise ValueError("manifest_not_object")
            item = _prepare_candidate(entry, manifest, path)
        except (OSError, ValueError, TypeError):
            item = _prepare_candidate(entry, {}, path)
            item["_eliminated_reason"] = "manifest_unreadable"
        manifest = item["manifest_data"]
        if item["_eliminated_reason"] is None and tag is not None and manifest.get("tag") != tag:
            item["_eliminated_reason"] = "tag_mismatch"
        if item["_eliminated_reason"] is None and cutoff is not None:
            try:
                available = _datetime(item["available_at"])
            except ValueError:
                available = None
            if available is None:
                item["_eliminated_reason"] = "available_at_unknown"
            elif available > cutoff:
                item["_eliminated_reason"] = "after_as_of"
        loaded.append(item)

    eligible = [item for item in loaded if item["_eliminated_reason"] is None]
    selected = None
    raw_rows: list[dict[str, Any]] = []
    rejected = 0
    while eligible:
        selected = select_version(eligible, cutoff)
        if selected is None:
            break
        try:
            raw_rows, rejected = _read_rows(selected["manifest_data"],
                                            Path(selected["manifest_path"]), root_path)
            break
        except (OSError, ValueError, TypeError, KeyError):
            selected["_eliminated_reason"] = "rows_unreadable"
            eligible.remove(selected)
            selected = None

    for item in eligible:
        if item is not selected and item["_eliminated_reason"] is None:
            item["_eliminated_reason"] = "lower_priority"
    candidate_info = [_candidate_info(item, item["_eliminated_reason"]) for item in loaded]
    if selected is None:
        return DataSet([], _empty_meta(candidate_info))

    manifest = selected["manifest_data"]
    source = selected["source"]
    if spec.fields:
        resolution = resolve_adapter(spec, manifest, source, sample_rows=raw_rows[:200])
    else:
        resolution = AdapterResolution(None, "unregistered")
    output_rows = raw_rows
    flag_counts: Counter[str] = Counter()
    unmapped_keys: Counter[str] = Counter()
    meta_extras: dict[str, Any] = {}
    if view == "canonical" and spec.fields:
        context = {"trade_date": trade_date}
        if tag is not None:
            context["tag"] = tag
        output_rows = []
        for row in raw_rows:
            canonical = canonicalize_row(spec, resolution.adapter, row, context)
            output_rows.append(canonical.values)
            flag_counts.update(canonical.flags.keys())
            unmapped_keys.update(canonical.unmapped_keys)
    elif view == "canonical":
        meta_extras["canonical"] = "UNREGISTERED"

    gate_status = selected["gate_status"]
    business_status = selected["business_status"]
    reasons = []
    if gate_status == "FAIL":
        reasons.append("gate_fail")
    if business_status != "FORMAL":
        reasons.append(f"business_status:{business_status or 'UNKNOWN'}")
    if flag_counts:
        reasons.append("canonical_flags")
    meta = {
        "status": "DEGRADED" if reasons else "OK",
        "version": selected["version"],
        "source": source, "adapter": resolution.adapter,
        "adapter_resolved_by": resolution.resolved_by,
        "business_status": business_status, "available_at": selected["available_at"],
        "gate_status": gate_status,
        "coverage": selected["coverage"], "candidates": candidate_info,
        "rows_total": len(raw_rows) + rejected, "rejected": rejected,
        "flag_counts": dict(sorted(flag_counts.items())),
        "unmapped_keys": dict(sorted(unmapped_keys.items())), "reasons": reasons,
        **meta_extras,
    }
    return DataSet(output_rows, meta)
