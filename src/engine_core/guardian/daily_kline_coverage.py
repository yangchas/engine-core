"""Where does the guardian look to decide that a daily_kline day is covered? (C567)

One interface, ``CoverageSource.coverage(dates)``; the TD implementation is primary, the catalog implementation is the
read-only fallback. A TD that cannot be read yields UNKNOWN (never "missing"): the caller falls back to the catalog and
labels the result with its origin, so an outage cannot trigger a mass re-fetch.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

OK = "OK"
MISSING = "MISSING"          # no usable version at all
INCOMPLETE = "INCOMPLETE"    # version metadata exists but the stored rows do not match it
LOW_QUALITY = "LOW_QUALITY"  # rows present, but only PROVISIONAL/DEGRADED/PARTIAL (needs an upgrade)
UNKNOWN = "UNKNOWN"          # the source could not be read


@dataclass(frozen=True)
class DayCoverage:
    date: str
    state: str
    origin: str                      # "td" | "catalog"
    version: str = ""
    business_status: str = ""
    rank_level: int = 0
    needs_upgrade: bool = False
    rows: int = 0
    reason: str = ""

    @property
    def present(self) -> bool:
        return self.state in {OK, LOW_QUALITY}

    def as_dict(self) -> dict[str, Any]:
        return {"date": self.date, "state": self.state, "origin": self.origin, "version": self.version,
                "business_status": self.business_status, "needs_upgrade": self.needs_upgrade, "rows": self.rows,
                "reason": self.reason}


class CoverageSource(Protocol):
    origin: str

    def coverage(self, dates: Sequence[str]) -> dict[str, DayCoverage]: ...


class TdCoverageSource:
    """Reads the Core TD version table and the stored row counts. Raises TdUnavailable when TD cannot be read."""
    origin = "td"

    def __init__(self, store: Any):
        self.store = store

    def coverage(self, dates: Sequence[str]) -> dict[str, DayCoverage]:
        from ..data_pipeline.td_sink import pick_best
        result: dict[str, DayCoverage] = {}
        if not self.store.schema_exists():
            return {day: DayCoverage(day, MISSING, self.origin, reason="td_schema_absent") for day in dates}
        counts = self.store.day_counts(list(dates))
        for day in dates:
            current = pick_best(self.store.read_versions(day))
            if current is None:
                state = MISSING if counts.get(day, 0) == 0 else INCOMPLETE
                reason = "no_version_metadata" if state == MISSING else "rows_without_version_metadata"
                result[day] = DayCoverage(day, state, self.origin, rows=counts.get(day, 0), reason=reason)
                continue
            expected = int(current.get("row_count") or 0)
            rows = counts.get(day, 0)
            base = dict(version=str(current["version"]), business_status=str(current.get("business_status") or ""),
                        rank_level=int(current.get("rank_level") or 0),
                        needs_upgrade=bool(current.get("needs_upgrade")), rows=rows)
            if rows != expected or expected <= 0:
                result[day] = DayCoverage(day, INCOMPLETE, self.origin, reason=f"rows={rows}!=version_rows={expected}",
                                          **base)
            elif base["needs_upgrade"]:
                result[day] = DayCoverage(day, LOW_QUALITY, self.origin, reason="only_" + (
                    base["business_status"] or "unlabeled").lower() + "_version", **base)
            else:
                result[day] = DayCoverage(day, OK, self.origin, **base)
        return result


class CatalogCoverageSource:
    """Read-only catalog judgement (the pre-C567 behaviour), used as the labelled fallback."""
    origin = "catalog"

    def __init__(self, catalog: Mapping[str, Any], now: datetime):
        self.catalog = catalog
        self.now = now

    def coverage(self, dates: Sequence[str]) -> dict[str, DayCoverage]:
        from .registry import catalog_manifest, catalog_partition_present, daily_kline_needs_upgrade
        result = {}
        for day in dates:
            manifest = catalog_manifest(self.catalog, "daily_kline", day)
            present = catalog_partition_present(self.catalog, "daily_kline", day, self.now)
            upgrade = daily_kline_needs_upgrade(manifest)
            state = MISSING if not present else (LOW_QUALITY if upgrade else OK)
            result[day] = DayCoverage(day, state, self.origin, version=str((manifest or {}).get("version", "")),
                                      business_status=str((manifest or {}).get("business_status") or ""),
                                      needs_upgrade=upgrade, rows=int((manifest or {}).get("row_count") or 0))
        return result


@dataclass(frozen=True)
class CoverageResult:
    days: dict[str, DayCoverage]
    origin: str                   # "td" | "catalog_fallback" | "catalog"
    unknown_reason: str = ""      # why TD could not be used (empty when origin == "td")

    def as_dict(self) -> dict[str, Any]:
        return {"origin": self.origin, "unknown_reason": self.unknown_reason,
                "days": {day: cov.as_dict() for day, cov in sorted(self.days.items())}}


def resolve_coverage(dates: Sequence[str], primary: CoverageSource | None, fallback: CoverageSource) -> CoverageResult:
    """TD first. Any TD read failure -> catalog fallback, labelled; TD not configured -> plain catalog."""
    from ..data_pipeline.td_sink import TdUnavailable
    if primary is None:
        return CoverageResult(fallback.coverage(dates), "catalog", "td_not_configured")
    try:
        return CoverageResult(primary.coverage(dates), primary.origin)
    except TdUnavailable as exc:
        return CoverageResult(fallback.coverage(dates), "catalog_fallback", str(exc)[:200])


# ---------------------------------------------------------------------------------------------------------------------
# Settings and wiring
# ---------------------------------------------------------------------------------------------------------------------
def td_settings(*, database: str | None = None) -> dict[str, Any] | None:
    """Registry ``daily_kline.td_sink`` settings (+ ``CORE_TD_DATABASE`` override); None when TD is not configured."""
    from .registry import load_registry
    item = next((entry for entry in load_registry() if entry["id"] == "daily_kline"), None)
    settings = dict((item or {}).get("td_sink") or {})
    if not settings or not settings.get("self_check", False):
        return None
    name = database or os.environ.get("CORE_TD_DATABASE") or settings.get("database")
    settings["database"] = name
    return settings


def make_store(settings: Mapping[str, Any]):
    from ..data_pipeline.td_sink import TaosStore
    return TaosStore(str(settings["database"]))


def build_primary_source(settings: Mapping[str, Any] | None, store: Any = None) -> CoverageSource | None:
    if settings is None:
        return None
    if store is None:
        if not os.environ.get("TD_CONFIG"):
            return None  # no connection configured in this process: plain catalog, not an outage
        store = make_store(settings)
    return TdCoverageSource(store)


# alert-once bookkeeping for "TD unreadable" ----------------------------------------------------------------------------
def _state_path(root: Path) -> Path:
    return root / "guardian-native" / "td_selfcheck_state.json"


def note_td_readability(root: Path, result: CoverageResult, now: datetime) -> dict[str, Any] | None:
    """Return an alert payload the first time TD is unreadable; clear the marker when it is readable again."""
    from ..data_pipeline.storage import _atomic_write, canonical_json
    path = _state_path(root)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if result.origin == "td":
        if state.get("unreadable_since"):
            _atomic_write(path, canonical_json({"unreadable_since": None, "recovered_at": now.isoformat()}) + b"\n")
        return None
    if result.origin != "catalog_fallback":
        return None
    if state.get("unreadable_since"):
        return None
    _atomic_write(path, canonical_json({"unreadable_since": now.isoformat(), "reason": result.unknown_reason}) + b"\n")
    return {"event": "daily_kline_td_unreadable", "reason": "daily_kline_td_selfcheck_unknown_catalog_fallback",
            "error": result.unknown_reason}
