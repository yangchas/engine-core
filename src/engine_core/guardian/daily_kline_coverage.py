"""TD-only daily-kline coverage and per-symbol watermark planning."""
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
    origin: str = "td"
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

    def latest_watermarks(self) -> dict[str, str]:
        """Use exactly one TD aggregate for every symbol; callers must not loop over per-symbol queries."""
        if not self.store.schema_exists():
            return {}
        return self.store.latest_watermarks()

    def coverage(self, dates: Sequence[str], watermarks: Mapping[str, str] | None = None,
                 known_missing: Mapping[str, str] | None = None) -> dict[str, DayCoverage]:
        from ..data_pipeline.td_sink import pick_best
        result: dict[str, DayCoverage] = {}
        if not self.store.schema_exists():
            return {day: DayCoverage(day, MISSING, self.origin, reason="td_schema_absent") for day in dates}
        counts = self.store.day_counts(list(dates))
        watermarks = watermarks or {}
        ignored_watermarks = {str(symbol).zfill(6) for symbol, category in (known_missing or {}).items()
                              if category == "SOURCE_UNAVAILABLE"}
        latest_day = max(dates, default="")
        for day in dates:
            current = pick_best(self.store.read_versions(day))
            if current is None:
                state = MISSING if counts.get(day, 0) == 0 else INCOMPLETE
                reason = "no_version_metadata" if state == MISSING else "rows_without_version_metadata"
                result[day] = DayCoverage(day, state, self.origin, rows=counts.get(day, 0), reason=reason)
                continue
            expected = int(current.get("row_count") or 0)
            rows = counts.get(day, 0)
            provider = str(current.get("provider") or current.get("source") or "").lower()
            base = dict(version=str(current["version"]), business_status=str(current.get("business_status") or ""),
                        rank_level=int(current.get("rank_level") or 0),
                        needs_upgrade=bool(current.get("needs_upgrade")), rows=rows)
            if rows != expected or expected <= 0:
                result[day] = DayCoverage(day, INCOMPLETE, self.origin, reason=f"rows={rows}!=version_rows={expected}",
                                          **base)
            elif provider != "baostock":
                result[day] = DayCoverage(day, LOW_QUALITY, self.origin,
                                          reason="non_baostock_provider:" + (provider or "unknown"), **base)
            elif day == latest_day and any(value < day for symbol, value in watermarks.items()
                                           if str(symbol).zfill(6) not in ignored_watermarks):
                result[day] = DayCoverage(day, INCOMPLETE, self.origin, reason="symbol_watermark_behind_target",
                                          **base)
            elif base["needs_upgrade"]:
                result[day] = DayCoverage(day, LOW_QUALITY, self.origin, reason="only_" + (
                    base["business_status"] or "unlabeled").lower() + "_version", **base)
            else:
                result[day] = DayCoverage(day, OK, self.origin, **base)
        return result


def build_gap_fill_plan(watermarks: Mapping[str, str], target_date: str, trading_dates: Sequence[str],
                        history_min_trading_days: int = 10, *,
                        day_states: Mapping[str, Any] | None = None,
                        symbols: Sequence[str] | None = None,
                        symbol_gap_dates: Mapping[str, Sequence[str]] | None = None) -> list[dict[str, Any]]:
    """Plan one Baostock date range per lagging symbol; nearest missing dates are listed first."""
    from datetime import date
    target = date.fromisoformat(target_date).isoformat()
    sessions = sorted({date.fromisoformat(str(day)).isoformat() for day in trading_dates if str(day) <= target})
    if target not in sessions:
        raise ValueError("target_not_in_trade_calendar:" + target)
    depth = max(1, int(history_min_trading_days))
    floor_index = max(0, len(sessions) - depth)
    gap_days = []
    for day, value in (day_states or {}).items():
        state = getattr(value, "state", value.get("state") if isinstance(value, Mapping) else value)
        reason = getattr(value, "reason", value.get("reason", "") if isinstance(value, Mapping) else "")
        # A target-day watermark lag is already represented per symbol below. Forcing that whole day
        # would turn one lagging symbol into a full-universe request.
        full_day_gap = (state in {MISSING, LOW_QUALITY} or
                        (state == INCOMPLETE and reason != "symbol_watermark_behind_target"))
        if day in sessions and full_day_gap:
            gap_days.append(str(day))
    forced_index = min((sessions.index(day) for day in gap_days), default=len(sessions))
    ranges: list[dict[str, Any]] = []
    known = {str(symbol): str(watermark)[:10] for symbol, watermark in watermarks.items()}
    universe = ({str(value) for value in symbols} if symbols is not None else set(known))
    for symbol in sorted(universe):
        last = known.get(symbol, "")
        later = [index for index, day in enumerate(sessions) if day > last]
        start_index = max(floor_index, later[0] if later else len(sessions))
        # A missing or provisional whole-day partition may sit before a symbol's latest watermark.
        # Re-read from that earliest affected session through the target so TD + Baostock can rebuild it.
        start_index = min(start_index, forced_index)
        forced_symbol_dates = [str(day) for day in (symbol_gap_dates or {}).get(symbol, ()) if str(day) in sessions]
        if forced_symbol_dates:
            start_index = min(start_index, min(sessions.index(day) for day in forced_symbol_dates))
        if start_index < len(sessions):
            ranges.append({"symbol": symbol, "watermark": last or None,
                           "start_date": sessions[start_index], "end_date": target,
                           "trade_dates": sessions[start_index:],
                           "gap_dates": [day for day in gap_days if day >= sessions[start_index]] +
                                        [day for day in forced_symbol_dates if day >= sessions[start_index]]})
    ranges.sort(key=lambda item: (item["start_date"], item["symbol"]), reverse=True)
    return ranges


@dataclass(frozen=True)
class CoverageResult:
    days: dict[str, DayCoverage]
    origin: str
    unknown_reason: str = ""
    watermarks: dict[str, str] | None = None
    gap_fill_plan: list[dict[str, Any]] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"origin": self.origin, "unknown_reason": self.unknown_reason,
                "watermarks": self.watermarks or {}, "gap_fill_plan": self.gap_fill_plan or [],
                "days": {day: cov.as_dict() for day, cov in sorted(self.days.items())}}


def resolve_coverage(dates: Sequence[str], primary: CoverageSource | None,
                     trading_dates: Sequence[str], history_min_trading_days: int = 10,
                     known_missing: Mapping[str, str] | None = None) -> CoverageResult:
    """Read TD once for all-symbol watermarks. A TD outage stays UNKNOWN and never starts a mass fetch."""
    from ..data_pipeline.td_sink import TdUnavailable
    if primary is None:
        return CoverageResult({day: DayCoverage(day, UNKNOWN, "unknown", reason="td_not_configured")
                               for day in dates}, "unknown", "td_not_configured")
    try:
        watermarks = primary.latest_watermarks() if isinstance(primary, TdCoverageSource) else {}
        days = (primary.coverage(dates, watermarks, known_missing)
                if isinstance(primary, TdCoverageSource) else primary.coverage(dates))
        plan = (build_gap_fill_plan(watermarks, max(dates, default=""), trading_dates,
                                    history_min_trading_days, day_states=days)
                if dates else [])
        return CoverageResult(days, primary.origin, watermarks=watermarks, gap_fill_plan=plan)
    except TdUnavailable as exc:
        return CoverageResult({day: DayCoverage(day, UNKNOWN, "unknown", reason=str(exc)[:200]) for day in dates},
                              "unknown", str(exc)[:200])


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
    if result.origin != "unknown":
        return None
    if state.get("unreadable_since"):
        return None
    _atomic_write(path, canonical_json({"unreadable_since": now.isoformat(), "reason": result.unknown_reason}) + b"\n")
    return {"event": "daily_kline_td_unreadable", "reason": "daily_kline_td_selfcheck_unknown",
            "error": result.unknown_reason}
