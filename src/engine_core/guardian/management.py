"""Local versioned schedule and append-only guardian control records."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .scheduler import EventSpec, TZ, _atomic_json, default_events

SCHEMA_VERSION = 2
HANDLERS = {"auction_report", "auction_watch", "baostock_probe", "boot_apply_plan",
            "boot_data_health", "boot_reconcile", "boot_trade_day", "calendar_refresh",
            "close_capture", "close_report", "fetch_item", "intraday_data", "kaipan_fetch",
            "kaipan_hot_capture", "night_report", "open_report", "postmarket_data",
            "premarket_data", "snapshot_check"}
GATES = {event.gate for event in default_events()} | {"daily_kline_ready"}


def schedule_payload(revision: int = 1) -> dict[str, Any]:
    payload = json.loads(Path(__file__).with_name("schedule.json").read_text(encoding="utf-8"))
    payload["revision"] = revision
    return payload


def validate_schedule(payload: dict[str, Any], previous: dict[str, Any] | None = None) -> tuple[EventSpec, ...]:
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("timezone") != "Asia/Shanghai":
        raise ValueError("schedule_schema_or_timezone_invalid")
    if not isinstance(payload.get("revision"), int) or payload["revision"] < 1:
        raise ValueError("schedule_revision_invalid")
    global_config = payload.get("global")
    if not isinstance(global_config, dict) or not isinstance(global_config.get("phases"), list):
        raise ValueError("schedule_global_invalid")
    raw_events = payload.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        raise ValueError("schedule_events_invalid")
    events = []
    ids = set()
    for raw in raw_events:
        if not isinstance(raw, dict):
            raise ValueError("schedule_event_invalid")
        if raw.get("handler") not in HANDLERS:
            raise ValueError("schedule_handler_unknown:" + str(raw.get("handler")) + ";available=" + ",".join(sorted(HANDLERS)))
        if raw.get("gate") not in GATES:
            raise ValueError("schedule_gate_unknown:" + str(raw.get("gate")))
        raw = dict(raw)
        raw["depends_on"] = tuple(raw.get("depends_on") or ())
        raw["after"] = tuple(raw.get("after") or ())
        if raw.get("phases") is not None:
            raw["phases"] = tuple(raw["phases"])
        try:
            event = EventSpec(**raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("schedule_event_invalid") from exc
        if event.event_id in ids:
            raise ValueError("schedule_duplicate_event_id")
        ids.add(event.event_id)
        if event.max_attempts > 10 or event.timeout_seconds > 3600 or event.retry_max_seconds > 3600:
            raise ValueError("schedule_bounds_invalid")
        if event.data_date_offset not in {-1, 0}:
            raise ValueError("schedule_date_offset_invalid")
        if event.available_after is not None:
            from .scheduler import _clock
            _clock(event.available_after)
        events.append(event)
    graph = {e.event_id: set(e.depends_on) | set(e.after) for e in events}
    if any(dep not in graph for deps in graph.values() for dep in deps):
        raise ValueError("schedule_dependency_unknown")
    visiting, visited = set(), set()
    def visit(node):
        if node in visiting:
            raise ValueError("schedule_dependency_cycle")
        if node in visited:
            return
        visiting.add(node)
        for dep in graph[node]:
            visit(dep)
        visiting.remove(node)
        visited.add(node)
    for node in graph:
        visit(node)
    if previous:
        old = {e["event_id"]: e for e in previous.get("events", [])}
        new = {e.event_id: e for e in events}
        for old_id, old_event in old.items():
            for new_id, new_event in new.items():
                if old_id != new_id and (old_event.get("start"), old_event.get("handler")) == (new_event.start, new_event.handler):
                    raise ValueError("schedule_alias_id_rejected")
    return tuple(events)


def load_schedule(path: Path, *, create_default: bool = True) -> tuple[tuple[EventSpec, ...], dict[str, Any]]:
    if not path.exists():
        if not create_default:
            raise ValueError("schedule_missing")
        payload = schedule_payload()
        _atomic_json(path, payload)
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o600)
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
    return validate_schedule(payload), payload


def atomic_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(path, payload)
    path.chmod(0o600)


def append_record(root: Path, record: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    path = root / "control.jsonl"
    lock_path = root / ".control.lock"
    with lock_path.open("a+") as lock:
        import fcntl
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        seq = 1
        if path.exists():
            with path.open("rb") as stream:
                for line in stream:
                    try:
                        seq = max(seq, int(json.loads(line).get("seq", 0)) + 1)
                    except (ValueError, TypeError):
                        raise ValueError("control_log_corrupt")
        record = {"schema_version": 1, "seq": seq, **record}
        with path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            out.flush()
            os.fsync(out.fileno())
        path.chmod(0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def schedule_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
