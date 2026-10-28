"""Small persistent wall-clock scheduler for Core-owned daily tasks.

The scheduler only invokes injected handlers.  It does not connect to Redis,
TD, Kaipan, Wencai, or mail itself; those effects belong behind explicit CLI
flags and caller supplied handler adapters.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import subprocess
import sys
import threading
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, time as day_time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventSpec:
    """One schedule event using the field names consumed by Go.

    The compatibility properties below derive the old Python views from the
    canonical fields: ``catchup`` maps to ``catchup_mode != 'skip'``;
    ``degraded_at_deadline`` maps to ``on_deadline == 'degrade'``; and
    ``phase`` uses the first restricted phase, otherwise the phase at ``start``
    (startup events use ``NIGHT``). ``retry_base_seconds`` parses
    ``retry_backoff`` and defaults to Go's ``eventRetryDelay`` base of 1 second;
    ``retry_max_seconds`` is that function's 5 minute cap. Go's separate
    ``retryBackoff`` path for ``retry_until_late`` uses the same configured
    duration with a 1 minute default and 10 minute cap.
    """

    event_id: str
    start: str
    deadline: str
    gate: str
    handler: str
    max_attempts: int = 3
    timeout_seconds: int = 300
    depends_on: tuple[str, ...] = ()
    available_after: str | None = None
    data_date_offset: int = 0
    trigger_type: str = "window"
    order: int | None = None
    after: tuple[str, ...] = ()
    late_until: str | None = None
    late_until_date_offset_days: int = 0
    late_until_date_offset_error_codes: tuple[str, ...] = ()
    late_until_clock_by_error_code: Mapping[str, str] | None = None
    cost: str | None = None
    phases: tuple[str, ...] | None = None
    on_phase_mismatch: str | None = None
    on_start: str | None = None
    retry_until_late: bool = False
    retry_backoff: str | None = None
    silent_until_last: bool = False
    on_deadline: str = "missed"
    catchup_mode: str = "skip"
    enabled: bool = True
    calendar: str = "trading_day"
    args: Mapping[str, Any] | None = None

    def __post_init__(self):
        if self.max_attempts < 1 or self.timeout_seconds < 1:
            raise ValueError("invalid_retry_or_timeout")
        if self.retry_backoff is not None:
            try:
                _go_duration_seconds(self.retry_backoff)
            except ValueError as exc:
                raise ValueError("invalid_retry_or_timeout") from exc
        if self.trigger_type not in {"window", "startup"}:
            raise ValueError("invalid_trigger_type")
        if self.trigger_type == "window" and _clock(self.start) > _clock(self.deadline):
            raise ValueError("event_deadline_before_start")
        if self.late_until_date_offset_days not in {0, 1} or \
                self.late_until_date_offset_days and not self.late_until:
            raise ValueError("invalid_late_until_date_offset")
        if self.late_until_date_offset_error_codes and self.late_until_date_offset_days == 0:
            raise ValueError("invalid_late_until_date_offset_error_codes")
        if any(not isinstance(code, str) or not code for code in self.late_until_date_offset_error_codes):
            raise ValueError("invalid_late_until_date_offset_error_codes")
        clock_overrides = {}
        for code, value in (self.late_until_clock_by_error_code or {}).items():
            if not isinstance(code, str) or not code or code.strip() != code or not isinstance(value, str):
                raise ValueError("invalid_late_until_clock_by_error_code")
            try:
                clock_overrides[code] = _clock(value).strftime("%H:%M:%S")
            except ValueError as exc:
                raise ValueError("invalid_late_until_clock_by_error_code") from exc
        object.__setattr__(self, "late_until_clock_by_error_code", clock_overrides)

    @property
    def catchup(self) -> bool:
        return self.catchup_mode != "skip"

    @property
    def degraded_at_deadline(self) -> bool:
        return self.on_deadline == "degrade"

    @property
    def phase(self) -> str:
        if self.trigger_type == "startup":
            return "NIGHT"
        if self.phases and "any" not in self.phases:
            return self.phases[0]
        return phase_at(self.start)

    @property
    def retry_base_seconds(self) -> float:
        return _go_duration_seconds(self.retry_backoff) if self.retry_backoff is not None else 1.0

    @property
    def retry_max_seconds(self) -> int:
        return 300


_GO_DURATION_TOKEN = re.compile(r"(?P<number>(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))(?P<unit>ns|us|µs|ms|s|m|h)")
_GO_DURATION_SCALE = {
    "ns": Decimal("0.000000001"),
    "us": Decimal("0.000001"),
    "µs": Decimal("0.000001"),
    "ms": Decimal("0.001"),
    "s": Decimal("1"),
    "m": Decimal("60"),
    "h": Decimal("3600"),
}


def _go_duration_seconds(value: str) -> float:
    """Parse positive Go time.Duration syntax into seconds."""
    if not isinstance(value, str) or not value:
        raise ValueError("invalid_retry_backoff")
    remaining = value[1:] if value.startswith("+") else value
    if not remaining:
        raise ValueError("invalid_retry_backoff")
    cursor = 0
    total = Decimal(0)
    while cursor < len(remaining):
        match = _GO_DURATION_TOKEN.match(remaining, cursor)
        if match is None:
            raise ValueError("invalid_retry_backoff")
        try:
            total += Decimal(match.group("number")) * _GO_DURATION_SCALE[match.group("unit")]
        except InvalidOperation as exc:
            raise ValueError("invalid_retry_backoff") from exc
        cursor = match.end()
    nanoseconds = total * Decimal(1_000_000_000)
    # Go stores time.Duration as signed int64 nanoseconds and truncates any
    # fractional nanosecond during parsing.
    max_duration = Decimal(9_223_372_036_854_775_807)
    if nanoseconds <= 0 or nanoseconds > max_duration or int(nanoseconds) <= 0:
        raise ValueError("invalid_retry_backoff")
    return float(int(nanoseconds) / 1_000_000_000)


def _clock(value: str) -> day_time:
    return day_time.fromisoformat(value)


def phase_at(value: datetime | str, phases: Iterable[Mapping[str, str]] | None = None) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat("2000-01-01T" + value)
    local = value.astimezone(TZ) if value.tzinfo else value.replace(tzinfo=TZ)
    if phases is None:
        phases = _schedule_source()["global"]["phases"]
    current = local.hour * 3600 + local.minute * 60 + local.second
    for phase in phases:
        start_value = str(phase["start"])
        start = 86400 if start_value == "24:00" else _clock(start_value if len(start_value) == 8 else start_value + ":00").hour * 3600 + _clock(start_value if len(start_value) == 8 else start_value + ":00").minute * 60
        end_value = str(phase["end"])
        end = 86400 if end_value == "24:00" else _clock(end_value if len(end_value) == 8 else end_value + ":00").hour * 3600 + _clock(end_value if len(end_value) == 8 else end_value + ":00").minute * 60
        if start <= current < end:
            return str(phase["name"])
    return "UNKNOWN"


def _schedule_source(path: str | Path | None = None) -> dict[str, Any]:
    source = Path(path) if path else Path(__file__).with_name("schedule.json")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema_version") not in {2, 3}:
        raise ValueError("schedule_schema_invalid")
    return payload


def default_events(path: str | Path | None = None) -> list[EventSpec]:
    payload = _schedule_source(path)
    if payload["schema_version"] == 3:
        from .management import validate_schedule
        return list(validate_schedule(payload))
    events = []
    for raw in payload["events"]:
        values = dict(raw)
        values["depends_on"] = tuple(values.get("depends_on") or ())
        values["after"] = tuple(values.get("after") or ())
        if values.get("phases") is not None:
            values["phases"] = tuple(values["phases"])
        events.append(EventSpec(**values))
    return events


class FileInstanceLock:
    """Non-blocking flock guard for a single local guardian process."""
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._file.close()
            self._file = None
            raise RuntimeError("already_running") from exc

    def release(self) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


class Guardian:
    def __init__(self, events: Iterable[EventSpec], *, calendar_dates: Iterable[str] | None,
                 state_root: str | Path, gates: Mapping[str, Callable[..., bool]] | None = None,
                 handlers: Mapping[str, Callable[..., Any]] | None = None,
                 dry_run: bool = False, context: Mapping[str, Any] | None = None,
                 schedule_path: str | Path | None = None, config_revision: int = 1,
                 config_payload: Mapping[str, Any] | None = None):
        self.events = tuple(events)
        self.calendar_coverage = (getattr(calendar_dates, "coverage_start", None),
                                  getattr(calendar_dates, "coverage_end", None))
        self.calendar_dates = None if calendar_dates is None else frozenset(calendar_dates)
        self.state_root = Path(state_root)
        self.gates = dict(gates or {})
        self.handlers = dict(handlers or {})
        self.dry_run = dry_run
        self.context = dict(context or {})
        self._states: dict[str, dict[str, Any]] = {}
        self.degraded = self.calendar_dates is None
        self._loaded_date: str | None = None
        self._dirty = False
        self._dry_seen: set[tuple[str, str]] = set()
        self.paused = False
        self.config_revision = 1
        self.schedule_path = Path(schedule_path) if schedule_path else None
        self.config_revision = config_revision
        self._config_payload = dict(config_payload or {})

    def phase(self, value: datetime | str) -> str:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        local = value.astimezone(TZ) if value.tzinfo else value.replace(tzinfo=TZ)
        if self.calendar_dates is None:
            return "UNKNOWN_CALENDAR"
        start, end = self.calendar_coverage
        if start and end and not start <= local.date().isoformat() <= end:
            return "UNKNOWN_CALENDAR"
        return phase_at(local, (self._config_payload.get("global") or {}).get("phases")) if local.date().isoformat() in self.calendar_dates else "HOLIDAY"

    def status(self, trade_date: str, event_id: str) -> str:
        if self._loaded_date != trade_date:
            self._load(trade_date)
        return self._states.get(event_id, {}).get("status", "PENDING")

    def tick(self, now: datetime) -> list[dict[str, Any]]:
        local = now.astimezone(TZ) if now.tzinfo else now.replace(tzinfo=TZ)
        trade_date = local.date().isoformat()
        actions: list[dict[str, Any]] = []
        self._load(trade_date)
        if not self._load_error:
            self._consume_control(trade_date, local, actions)
            self._reload_schedule(actions)
        if self.paused:
            if self._dirty:
                self._save(trade_date, local)
            return actions + [{"status": "PAUSED"}]
        # Calendar maintenance is a control-plane event: it must be considered
        # before calendar coverage and holiday checks, and always uses the same
        # timeout and dry-run path as other scheduled work.
        for event in self.events:
            if event.trigger_type != "window" or not event.enabled:
                continue
            if event.handler != "calendar_refresh":
                continue
            row = self._states.get(event.event_id, {"status": "PENDING", "attempts": 0})
            if row.get("status") in {"SUCCEEDED", "SUCCEEDED_DEGRADED", "MISSED", "CIRCUIT_OPEN"}:
                continue
            event_start = datetime.combine(local.date(), _clock(event.start), TZ)
            event_deadline = datetime.combine(local.date(), _clock(event.deadline), TZ)
            if local < event_start or local > event_deadline:
                continue
            if self.dry_run:
                token = (trade_date, event.event_id)
                if token not in self._dry_seen:
                    self._dry_seen.add(token)
                    actions.append({"event": event.event_id, "status": "WOULD_RUN"})
                continue
            row = self._states.setdefault(event.event_id, {"status": "PENDING", "attempts": 0})
            self._run(event, local, row, False, actions)
            if self._dirty:
                self._save(trade_date, local)
        if self.calendar_dates is None:
            self.degraded = True
            return actions + [{"status": "CALENDAR_UNAVAILABLE", "at": local.isoformat()}]
        if trade_date not in self.calendar_dates:
            start, end = self.calendar_coverage
            if start and end and not start <= trade_date <= end:
                return actions + [{"status": "CALENDAR_COVERAGE_UNKNOWN", "at": local.isoformat()}]
            return actions + [{"status": "HOLIDAY", "at": local.isoformat()}]
        if self.degraded and self._load_error:
            return [{"status": "DEGRADED_STATE", "reason": self._load_error}]
        for event in self.events:
            if event.trigger_type != "window" or not event.enabled:
                continue
            status = self._states.get(event.event_id, {}).get("status", "PENDING")
            if status == "STALE_RUNNING":
                self.degraded = True
                continue
            if status in {"SUCCEEDED", "SUCCEEDED_DEGRADED", "MISSED", "CIRCUIT_OPEN"}:
                continue
            event_start = datetime.combine(local.date(), _clock(event.start), TZ)
            event_deadline = datetime.combine(local.date(), _clock(event.deadline), TZ)
            if local < event_start:
                continue
            if local > event_deadline:
                if status not in {"SUCCEEDED", "SUCCEEDED_DEGRADED", "MISSED", "CIRCUIT_OPEN"}:
                    if self.dry_run:
                        token = (trade_date, event.event_id)
                        if token not in self._dry_seen:
                            self._dry_seen.add(token)
                            actions.append({"event": event.event_id, "status": "WOULD_MISS"})
                        continue
                    self._states.setdefault(event.event_id, {"attempts": 0}).update(
                        status="MISSED", completed_at=local.isoformat())
                    self._dirty = True
                    actions.append({"event": event.event_id, "status": "MISSED"})
                continue
            if event.available_after and local.time().replace(tzinfo=None) < _clock(event.available_after):
                continue
            if local > event_start and not event.catchup:
                continue
            row = self._states.setdefault(event.event_id, {"status": "PENDING", "attempts": 0})
            next_attempt = row.get("next_attempt_at")
            if next_attempt and local < datetime.fromisoformat(next_attempt):
                continue
            if any(self._states.get(dep, {}).get("status") not in {"SUCCEEDED", "SUCCEEDED_DEGRADED"}
                   for dep in event.depends_on):
                continue
            gate = self.gates.get(event.gate)
            try:
                gate_value = gate(event, local, self.context) if gate else False
                ready = (float(gate_value) >= 0.95 if event.gate == "auction_coverage"
                         and isinstance(gate_value, (int, float)) else bool(gate_value))
            except Exception as exc:
                ready = False
                row["gate_error"] = type(exc).__name__ + ":" + str(exc)
            degraded = not ready and local >= event_deadline and event.degraded_at_deadline
            if not ready and not degraded:
                if local >= event_deadline:
                    row.update(status="MISSED", completed_at=local.isoformat())
                    self._dirty = True
                    actions.append({"event": event.event_id, "status": "MISSED"})
                elif row.get("status") != "WAITING":
                    row["status"] = "WAITING"
                    self._dirty = True
                continue
            if self.dry_run:
                token = (trade_date, event.event_id)
                if token in self._dry_seen:
                    continue
                self._dry_seen.add(token)
                actions.append({"event": event.event_id, "status": "WOULD_RUN" if ready else "WOULD_RUN_DEGRADED"})
                continue
            self._run(event, local, row, degraded, actions)
        if self._dirty and not self.dry_run:
            self._save(trade_date, local)
        return actions

    def _run(self, event: EventSpec, now: datetime, row: dict[str, Any], degraded: bool,
             actions: list[dict[str, Any]]) -> None:
        handler = self.handlers.get(event.handler)
        if handler is None:
            exc = RuntimeError("handler_not_configured:" + event.handler)
            self._failed(event, now, row, exc, actions)
            return
        row.update(status="RUNNING", started_at=now.isoformat(), attempts=int(row.get("attempts", 0)) + 1)
        self._dirty = True
        self._save(now.date().isoformat(), now)
        started = time.monotonic()
        try:
            def persist_worker(pid):
                row.update(worker_pid=pid, attempt_id=uuid.uuid4().hex)
                self._save(now.date().isoformat(), now)
            if event.handler == "calendar_refresh":
                result = _invoke_calendar_with_timeout(
                    handler, (event, now, self.context, degraded), event.timeout_seconds)
            else:
                result = _invoke_with_timeout(handler, (event, now, self.context, degraded),
                                              event.timeout_seconds, on_started=persist_worker)
            result_degraded = isinstance(result, str) and result.startswith("DEGRADED:")
            if event.handler == "calendar_refresh" and isinstance(result, str) and not result_degraded:
                try:
                    payload = json.loads(result)
                    self.calendar_dates = frozenset(payload.get("trading_dates", [])) or None
                    coverage = payload.get("calendar", {})
                    self.calendar_coverage = (coverage.get("coverage_start"), coverage.get("coverage_end"))
                    self.degraded = self.calendar_dates is None
                except (ValueError, TypeError):
                    result_degraded = True
            row.update(status="SUCCEEDED_DEGRADED" if degraded or result_degraded else "SUCCEEDED",
                        completed_at=now.isoformat(), duration_seconds=round(time.monotonic() - started, 3),
                        result=str(result)[:500])
            row.pop("worker_pid", None)
            row.pop("attempt_id", None)
            row.pop("next_attempt_at", None)
            actions.append({"event": event.event_id, "status": row["status"], "duration_seconds": row["duration_seconds"]})
            self._dirty = True
        except Exception as exc:
            self._failed(event, now, row, exc, actions)

    def _failed(self, event: EventSpec, now: datetime, row: dict[str, Any], exc: Exception,
                actions: list[dict[str, Any]]) -> None:
        attempts = int(row.get("attempts", 0))
        row.pop("worker_pid", None)
        row.pop("attempt_id", None)
        row["last_error"] = type(exc).__name__ + ":" + str(exc)
        if isinstance(exc, TimeoutError) or attempts >= event.max_attempts:
            row.update(status="CIRCUIT_OPEN", completed_at=now.isoformat())
            self.degraded = True
        else:
            delay = min(event.retry_max_seconds, event.retry_base_seconds * (2 ** max(0, attempts - 1)))
            row.update(status="RETRY_WAIT", next_attempt_at=(now + timedelta(seconds=delay)).isoformat())
        actions.append({"event": event.event_id, "status": row["status"], "error": row["last_error"]})
        self._dirty = True

    def _state_path(self, trade_date: str) -> Path:
        return self.state_root / trade_date / "state.json"

    def _load(self, trade_date: str) -> None:
        if self._loaded_date == trade_date:
            return
        self._states = {}
        self._loaded_date = trade_date
        self._dirty = False
        self._load_error = None
        try:
            raw = self._state_path(trade_date).read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            self.degraded = True
            self._load_error = "state_unreadable:" + type(exc).__name__
            return
        try:
            payload = json.loads(raw)
            if payload.get("trade_date") != trade_date or not isinstance(payload.get("events", {}), dict):
                raise ValueError("state_schema_invalid")
            self._states = payload.get("events", {})
            for row in self._states.values():
                if row.get("status") == "RUNNING":
                    row.update(status="STALE_RUNNING", recovery="worker_identity_unavailable")
                    self.degraded = True
                    self._dirty = True
        except (ValueError, AttributeError, TypeError) as exc:
            path = self._state_path(trade_date)
            backup = path.with_suffix(path.suffix + ".corrupt")
            if not backup.exists():
                backup.write_text(raw, encoding="utf-8")
            self.degraded = True
            self._load_error = "state_corrupt:" + type(exc).__name__

    def _save(self, trade_date: str, now: datetime) -> None:
        path = self._state_path(trade_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        heartbeat = {"trade_date": trade_date, "last_tick_at": now.isoformat(),
                     "phase": self.phase(now), "next_event": self._next_event(now),
                     "health": "DEGRADED" if self.degraded else "READY", "events": self._states,
                     "paused": self.paused, "config_revision": self.config_revision}
        _atomic_json(path, {"trade_date": trade_date, "events": self._states, "heartbeat": heartbeat})
        os.chmod(self.state_root, 0o700)
        os.chmod(path, 0o600)
        try:
            _atomic_json(self.state_root / "heartbeat.json", heartbeat)
            os.chmod(self.state_root / "heartbeat.json", 0o600)
        except OSError:
            _LOGGER.exception("guardian heartbeat write failed; retrying on next tick")
        self._dirty = False

    def heartbeat(self, now: datetime) -> dict[str, Any]:
        local = now.astimezone(TZ) if now.tzinfo else now.replace(tzinfo=TZ)
        trade_date = local.date().isoformat()
        if self._loaded_date != trade_date:
            self._load(trade_date)
        return {"trade_date": trade_date, "last_tick_at": local.isoformat(),
                "phase": self.phase(local), "next_event": self._next_event(local),
                "health": "DEGRADED" if self.degraded else "READY", "events": self._states,
                "paused": self.paused, "config_revision": self.config_revision}

    def write_heartbeat(self, now: datetime) -> None:
        """Persist a heartbeat independently of event-state changes."""
        payload = self.heartbeat(now)
        try:
            _atomic_json(self.state_root / "heartbeat.json", payload)
            os.chmod(self.state_root, 0o700)
            os.chmod(self.state_root / "heartbeat.json", 0o600)
        except OSError:
            _LOGGER.exception("guardian heartbeat write failed; retrying on next tick")

    def _consume_control(self, trade_date: str, now: datetime, actions: list[dict[str, Any]]) -> None:
        path = self.state_root / "control.jsonl"
        if not path.exists():
            return
        last_seq = int(self._states.get("_control", {}).get("last_seq", 0))
        try:
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, ValueError):
            self.degraded = True
            self._load_error = "control_log_corrupt"
            return
        from .management import append_record
        for record in rows:
            seq = int(record.get("seq", 0))
            if seq <= last_seq or record.get("type") != "REQUEST":
                continue
            verb, event_id = record.get("verb"), record.get("event_id")
            result = "APPLIED"
            if record.get("business_date") != trade_date and verb not in {"pause", "resume"}:
                result = "REJECTED"
            elif verb in {"trigger", "retry", "skip"} and record.get("expected_revision") not in {None, self.config_revision}:
                result = "REJECTED"
            elif event_id and event_id not in {event.event_id for event in self.events}:
                result = "REJECTED"
            elif verb in {"trigger", "retry", "skip"} and event_id:
                self._states.setdefault(event_id, {"status": "PENDING", "attempts": 0})
            if verb == "pause":
                self.paused = True if result == "APPLIED" else self.paused
            elif verb == "resume":
                self.paused = False if result == "APPLIED" else self.paused
            elif verb == "skip" and event_id in self._states:
                if result == "REJECTED" or self._states[event_id].get("status") in {"RUNNING", "SUCCEEDED", "SUCCEEDED_DEGRADED"}:
                    result = "REJECTED"
                else:
                    self._states[event_id].update(status="SKIPPED", reason=record.get("reason"))
            elif verb == "retry" and event_id in self._states:
                if result == "REJECTED" or self._states[event_id].get("status") not in {"RETRY_WAIT", "CIRCUIT_OPEN"}:
                    result = "REJECTED"
                else:
                    self._states[event_id].update(status="RETRY_WAIT", next_attempt_at=now.isoformat())
            elif verb == "trigger" and event_id in self._states and result != "REJECTED":
                self._states[event_id]["manual_requested"] = True
            elif verb not in {"trigger", "retry", "skip", "pause", "resume"}:
                result = "REJECTED"
            self._states.setdefault("_control", {})["last_seq"] = seq
            applied_ids = self._states["_control"].setdefault("applied_request_ids", [])
            if record.get("request_id") and record["request_id"] not in applied_ids:
                applied_ids.append(record["request_id"])
            self._dirty = True
            self._save(trade_date, now)
            append_record(self.state_root, {"type": result, "request_id": record.get("request_id"),
                                            "business_date": trade_date, "event_id": event_id,
                                            "verb": verb, "result": result,
                                            "applied_at": now.isoformat(), "reason": "state_policy" if result == "REJECTED" else None})
            actions.append({"request_id": record.get("request_id"), "status": result})

    def _reload_schedule(self, actions: list[dict[str, Any]]) -> None:
        if self.schedule_path is None or not self.schedule_path.exists():
            return
        payload = None
        try:
            payload = json.loads(self.schedule_path.read_text(encoding="utf-8"))
            if payload == self._config_payload:
                return
            from .management import schedule_hash, validate_schedule, append_record
            updated = validate_schedule(payload, self._config_payload or None)
            completed = {event_id for event_id, row in self._states.items()
                         if row.get("status") in {"SUCCEEDED", "SUCCEEDED_DEGRADED", "SKIPPED", "MISSED"}}
            if any(event.event_id in completed and event.event_id not in {e.event_id for e in self.events}
                   for event in updated):
                raise ValueError("schedule_completed_event_reintroduced")
            old_hash = schedule_hash(self._config_payload) if self._config_payload else None
            revision = payload.get("revision", 0)
            append_record(self.state_root, {"type": "CONFIG_APPLIED", "business_date": self._loaded_date,
                                            "old_hash": old_hash, "new_hash": schedule_hash(payload),
                                            "revision": revision, "result": "APPLIED"})
            self.events = updated
            self.config_revision = revision
            self._config_payload = payload
            actions.append({"status": "CONFIG_APPLIED", "revision": revision})
        except (OSError, ValueError, TypeError) as exc:
            self.degraded = True
            reason = str(exc)
            actions.append({"status": "CONFIG_REJECTED", "reason": reason})
            try:
                from .management import schedule_hash, append_record
                append_record(self.state_root, {
                    "type": "CONFIG_REJECTED", "business_date": self._loaded_date,
                    "old_hash": schedule_hash(self._config_payload) if self._config_payload else None,
                    "new_hash": schedule_hash(payload) if isinstance(payload, dict) else None,
                    "revision": payload.get("revision") if isinstance(payload, dict) else None,
                    "result": "REJECTED", "reason": reason,
                })
            except (OSError, ValueError, TypeError) as record_error:
                actions[-1]["record_error"] = str(record_error)

    def _next_event(self, now: datetime) -> str | None:
        for event in self.events:
            if event.trigger_type != "window" or not event.enabled:
                continue
            if self._states.get(event.event_id, {}).get("status") not in {"SUCCEEDED", "SUCCEEDED_DEGRADED", "MISSED", "CIRCUIT_OPEN"}:
                if datetime.combine(now.date(), _clock(event.start), TZ) >= now:
                    return event.event_id
        return None


def _invoke_with_timeout(handler: Callable[..., Any], args: tuple[Any, ...], timeout: int,
                         on_started: Callable[[int], None] | None = None) -> Any:
    """Run a handler in a killable child and reap it before returning timeout."""
    import multiprocessing
    ctx = multiprocessing.get_context("fork")
    output = ctx.Queue(maxsize=1)
    release = ctx.Event()
    def target(queue, ready, fn, call_args):
        os.setsid()
        ready.wait()
        try:
            queue.put((True, fn(*call_args)))
        except BaseException as exc:
            queue.put((False, (type(exc).__name__, str(exc))))
    worker = ctx.Process(target=target, args=(output, release, handler, args), daemon=False)
    worker.start()
    try:
        if on_started:
            on_started(worker.pid)
    except BaseException:
        worker.terminate()
        worker.join()
        raise
    finally:
        release.set()
    worker.join(timeout)
    if worker.is_alive():
        import signal
        try:
            os.killpg(worker.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        worker.join(1)
        if worker.is_alive() and hasattr(worker, "kill"):
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            worker.join()
        raise TimeoutError(f"handler_timeout_reaped:{timeout}s")
    try:
        ok, value = output.get_nowait()
    except Exception:
        raise RuntimeError("handler_worker_exit:" + str(worker.exitcode))
    if not ok:
        raise RuntimeError(f"{value[0]}:{value[1]}")
    return value


def _invoke_calendar_with_timeout(handler: Callable[..., Any], args: tuple[Any, ...], timeout: int) -> Any:
    """Bound calendar maintenance while keeping its durable alert route in this process."""
    import signal
    import threading
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        return _invoke_with_timeout(handler, args, timeout)
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(_signum, _frame):
        raise TimeoutError(f"handler_timeout:{timeout}s")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        return handler(*args)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        remaining, interval = previous_timer
        if remaining > 0:
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, remaining - (time.monotonic() - started)), interval)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=f".{os.getpid()}.{uuid.uuid4().hex}.tmp",
                                     dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise

