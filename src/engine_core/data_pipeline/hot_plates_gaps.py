"""Gap-only ledger for missed Kaipan hot-plate capture windows."""
from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .jobs import JobRunner
from .storage import ArtifactCatalog

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _parse_time(value: str, day: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _snapshot_manifests(root: Path, trade_date: str) -> list[dict[str, Any]]:
    paths = root.glob(f"hot_plates/trade_date={trade_date}/source=*/sha256-*/manifest.json")
    result = []
    for path in paths:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(item, dict) and item.get("captured_at"):
            item.setdefault("manifest_uri", str(path))
            result.append(item)
    return result


def _existing_gap_rows(catalog: ArtifactCatalog, trade_date: str) -> list[dict[str, Any]]:
    path = catalog.locate("hot_plates_gaps", trade_date)
    if path is None:
        return []
    return [dict(row) for row in catalog.read_rows(path)]


def prior_hot_plate_dates(root: str | Path, session_date: str, count: int) -> list[str]:
    """Return the configured most recent source sessions before the capture session."""
    if type(count) is not int or not 1 <= count <= 3:
        raise ValueError("hot_plates_history_days_must_be_between_1_and_3")
    from .trade_calendar import TradeCalendarStore

    prior = [value for value in TradeCalendarStore(root).all_trading_dates()
             if value < date.fromisoformat(session_date).isoformat()]
    if len(prior) < count:
        raise RuntimeError("hot_plates_calendar_history_unknown")
    return prior[-count:]


def history_event_id(trade_date: str) -> str:
    return f"kaipan_history_{date.fromisoformat(trade_date).isoformat().replace('-', '')}"


def history_capture_exists(root: str | Path, trade_date: str) -> bool:
    """Check all immutable versions for the history event; later snapshots do not hide it."""
    event_id = history_event_id(trade_date)
    catalog = ArtifactCatalog(root)
    for manifest in _snapshot_manifests(Path(root).expanduser(), trade_date):
        if manifest.get("event_id") == event_id:
            return True
        manifest_path = manifest.get("manifest_uri")
        if manifest_path:
            try:
                if any(row.get("event_id") == event_id
                       for row in catalog.read_rows(Path(manifest_path))):
                    return True
            except (OSError, ValueError, TypeError):
                continue
    return False


def record_hot_plates_gap(root: str | Path, trade_date: str, event_id: str,
                          scheduled_at: str, deadline: str, observed_at: str,
                          reason: str, *, persist: bool = True) -> dict[str, Any]:
    """Append one idempotent GAP row and reference the newest earlier snapshot as STALE."""
    day = date.fromisoformat(trade_date).isoformat()
    root_path = Path(root).expanduser()
    catalog = ArtifactCatalog(root_path)
    rows = _existing_gap_rows(catalog, day)
    previous = next((row for row in rows if row.get("event_id") == event_id), None)
    if previous is not None:
        return previous

    scheduled = _parse_time(scheduled_at, day)
    snapshots = []
    for manifest in _snapshot_manifests(root_path, day):
        try:
            captured = _parse_time(str(manifest["captured_at"]), day)
        except (KeyError, TypeError, ValueError):
            continue
        if captured < scheduled:
            snapshots.append((captured, manifest))
    stale = max(snapshots, key=lambda item: item[0])[1] if snapshots else None
    gap = {
        "date": day,
        "trade_date": day,
        "event_id": event_id,
        "scheduled_at": scheduled_at,
        "deadline": deadline,
        "observed_at": observed_at,
        "status": "GAP",
        "late_policy": "gap_only",
        "gap_reason": reason,
        "fallback_status": "STALE" if stale else "MISSING",
        "stale_version": stale.get("version") if stale else None,
        "stale_captured_at": stale.get("captured_at") if stale else None,
        "stale_manifest": stale.get("manifest_uri") if stale else None,
        "fallback_source": "none",
    }
    rows.append(gap)
    if not persist:
        return gap
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=SHANGHAI)
    now = observed.astimezone(SHANGHAI).isoformat()
    JobRunner(artifact_root=root_path).run(
        "hot_plates_gaps", day,
        fetch=lambda _day: {"rows": rows, "expected_count": len(rows)},
        now=now, source="guardian_gap_only", as_of=day, publish=False)
    return gap


def _event_datetimes(trade_date: str, event: Mapping[str, Any]) -> tuple[datetime, datetime] | None:
    at, deadline = event.get("at"), event.get("deadline")
    if not isinstance(at, str) or not isinstance(deadline, str):
        return None
    start_text = f"{trade_date}T{at}+08:00"
    deadline_text = f"{trade_date}T{deadline}+08:00"
    return _parse_time(start_text, trade_date), _parse_time(deadline_text, trade_date)


def reconcile_due_hot_plates_gaps(root: str | Path, trade_date: str, now: str,
                                  schedule_path: str | Path | None = None,
                                  *, persist: bool = True) -> list[dict[str, Any]]:
    """Record expired hot-plate windows with no snapshot; never calls a provider."""
    day = date.fromisoformat(trade_date).isoformat()
    path = Path(schedule_path) if schedule_path else Path(__file__).parents[1] / "guardian" / "schedule.json"
    schedule = json.loads(path.read_text(encoding="utf-8"))
    current = _parse_time(now.replace("Z", "+00:00"), day)
    manifests = _snapshot_manifests(Path(root).expanduser(), day)
    known_events = {row.get("event_id") for row in _existing_gap_rows(ArtifactCatalog(root), day)}
    gaps = []
    for event in schedule.get("events", []):
        if not isinstance(event, Mapping) or event.get("handler") != "kaipan_hot_capture":
            continue
        event_args = event.get("args")
        if isinstance(event_args, Mapping) and event_args.get("reconcile_only"):
            continue
        event_id = str(event.get("event_id", ""))
        datetimes = _event_datetimes(day, event)
        if not event_id or datetimes is None:
            continue
        scheduled, due = datetimes
        if current < due:
            continue
        history_days = event_args.get("hot_plates_history_days") if isinstance(event_args, Mapping) else None
        if history_days is not None:
            try:
                history_dates = prior_hot_plate_dates(root, day, history_days)
            except (RuntimeError, ValueError):
                # Without calendar evidence the source dates are unknown; do not invent a partition.
                continue
            for target_date in history_dates:
                history_id = history_event_id(target_date)
                if any(row.get("event_id") == history_id for row in
                       _existing_gap_rows(ArtifactCatalog(root), target_date)):
                    continue
                if history_capture_exists(root, target_date):
                    continue
                gaps.append(record_hot_plates_gap(
                    root, target_date, history_id, scheduled.isoformat(), due.isoformat(),
                    current.isoformat(), "history_window_missed", persist=persist))
            continue
        if event_id in known_events:
            continue
        captured = False
        for manifest in manifests:
            try:
                snap_at = _parse_time(str(manifest["captured_at"]), day)
            except (KeyError, TypeError, ValueError):
                continue
            if manifest.get("event_id") == event_id or scheduled <= snap_at <= due:
                captured = True
                break
        if not captured:
            gaps.append(record_hot_plates_gap(
                root, day, event_id, scheduled.isoformat(), due.isoformat(),
                current.isoformat(), "capture_missing_at_deadline", persist=persist))
            known_events.add(event_id)
    return gaps
