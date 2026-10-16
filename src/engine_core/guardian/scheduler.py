"""Small persistent wall-clock scheduler for Core-owned daily tasks.

The scheduler only invokes injected handlers.  It does not connect to Redis,
TD, Kaipan, Wencai, or mail itself; those effects belong behind explicit CLI
flags and caller supplied handler adapters.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class EventSpec:
    event_id: str
    phase: str
    start: str
    deadline: str
    gate: str
    handler: str
    max_attempts: int = 3
    retry_base_seconds: int = 5
    retry_max_seconds: int = 300
    catchup: bool = True
    degraded_at_deadline: bool = False
    timeout_seconds: int = 300
    depends_on: tuple[str, ...] = ()

    def __post_init__(self):
        if self.max_attempts < 1 or self.retry_base_seconds < 0 or self.timeout_seconds < 1:
            raise ValueError("invalid_retry_or_timeout")
        if _clock(self.start) > _clock(self.deadline):
            raise ValueError("event_deadline_before_start")


def _clock(value: str) -> day_time:
    return day_time.fromisoformat(value)


def phase_at(value: datetime | str) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat("2000-01-01T" + value)
    local = value.astimezone(TZ) if value.tzinfo else value.replace(tzinfo=TZ)
    t = local.time().replace(tzinfo=None)
    if t >= day_time(17, 30) or t < day_time(8, 30):
        return "NIGHT" if t < day_time(8, 30) else "POSTMARKET"
    if t < day_time(9, 15):
        return "PREMARKET"
    if t < day_time(9, 30):
        return "AUCTION"
    if t < day_time(15, 5):
        return "INTRADAY"
    return "POSTMARKET"


def default_events() -> list[EventSpec]:
    events = [
        EventSpec("startup_0830", "PREMARKET", "08:30:00", "08:45:00", "calendar_ready", "premarket_data"),
        EventSpec("kaipan_refresh_0850", "PREMARKET", "08:50:00", "09:05:00", "calendar_ready", "kaipan_fetch", max_attempts=1),
        EventSpec("premarket_slice_0850", "PREMARKET", "08:50:00", "09:10:00", "calendar_ready", "premarket_data"),
        EventSpec("startup_0900", "PREMARKET", "09:00:00", "09:10:00", "calendar_ready", "premarket_data"),
        EventSpec("auction_watch_0915", "AUCTION", "09:15:00", "09:20:00", "calendar_ready", "auction_watch"),
        EventSpec("auction_0920_check", "AUCTION", "09:20:03", "09:23:59", "snapshot_0920_ready", "snapshot_check", max_attempts=1),
        EventSpec("auction_0924_check", "AUCTION", "09:24:10", "09:24:59", "snapshot_0924_ready", "snapshot_check", max_attempts=1),
        EventSpec("auction_0925_check", "AUCTION", "09:25:10", "09:29:59", "snapshot_0925_ready", "snapshot_check", max_attempts=1),
        EventSpec("auction_report_0926", "AUCTION", "09:26:30", "09:29:00", "auction_coverage", "auction_report", degraded_at_deadline=True),
    ]
    hot_times = ("09:16:00", "09:21:00", "09:26:30", "09:28:30", "09:31:00", "09:33:00",
                 "09:40:00", "10:10:00", "10:20:00", "10:30:00", "10:45:00", "11:00:00",
                 "11:25:00", "13:30:00", "14:00:00", "14:30:00", "14:50:00", "15:05:00")
    for stamp in hot_times:
        tag = stamp.replace(":", "")
        end = (datetime.combine(date.today(), _clock(stamp)) + timedelta(minutes=2)).time().isoformat()
        hot_phase = "AUCTION" if stamp < "09:30:00" else ("INTRADAY" if stamp < "15:00:00" else "POSTMARKET")
        events.append(EventSpec(f"kaipan_hot_{tag}", hot_phase,
                                stamp, end, "calendar_ready", "kaipan_hot_capture", max_attempts=1,
                                timeout_seconds=120))
    events.extend([
        EventSpec("open_report_0932", "INTRADAY", "09:32:10", "09:35:00", "checkpoint_ready", "open_report", degraded_at_deadline=True),
        EventSpec("open_report_0938", "INTRADAY", "09:38:10", "09:41:00", "checkpoint_ready", "open_report", degraded_at_deadline=True),
        EventSpec("open_report_0945", "INTRADAY", "09:45:30", "09:49:00", "checkpoint_ready", "open_report", degraded_at_deadline=True),
    ])
    for minute in range(30, 60, 3):
        # The legacy loop analyzes near the first five seconds of each minute.
        if minute == 30:
            continue
        events.append(EventSpec(f"intraday_{minute:02d}", "INTRADAY", f"09:{minute:02d}:05", f"09:{minute:02d}:59",
                                "checkpoint_ready", "intraday_data", max_attempts=1))
    for hour in range(10, 15):
        for minute in (0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30, 33, 36, 39, 42, 45, 48, 51, 54, 57):
            stamp = f"{hour:02d}:{minute:02d}:05"
            events.append(EventSpec("intraday_" + stamp.replace(":", ""), "INTRADAY", stamp,
                                    f"{hour:02d}:{minute:02d}:59", "checkpoint_ready", "intraday_data", max_attempts=1))
    events.extend([
        EventSpec("close_mark_1505", "POSTMARKET", "15:05:00", "15:20:00", "calendar_ready", "close_capture"),
        EventSpec("baostock_probe_1530", "POSTMARKET", "15:30:00", "17:39:59", "calendar_ready", "baostock_probe", max_attempts=1),
        EventSpec("baostock_retry_1740", "POSTMARKET", "17:40:00", "18:29:59", "calendar_ready", "baostock_probe", max_attempts=1),
        EventSpec("baostock_retry_1830", "POSTMARKET", "18:30:00", "19:59:59", "calendar_ready", "baostock_probe", max_attempts=1),
        EventSpec("baostock_retry_2000", "POSTMARKET", "20:00:00", "20:30:00", "calendar_ready", "baostock_probe", max_attempts=1),
        EventSpec("postmarket_publish", "POSTMARKET", "17:40:00", "20:30:00", "daily_kline_ready", "postmarket_data", degraded_at_deadline=False),
        EventSpec("close_recap", "POSTMARKET", "17:40:00", "20:30:00", "daily_kline_ready", "close_report", degraded_at_deadline=False),
        EventSpec("night_recap", "NIGHT", "01:00:00", "08:29:59", "calendar_ready", "night_report"),
    ])
    return sorted(events, key=lambda item: (item.start, item.event_id))


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
                 dry_run: bool = False, context: Mapping[str, Any] | None = None):
        self.events = tuple(events)
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

    def phase(self, value: datetime | str) -> str:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        local = value.astimezone(TZ) if value.tzinfo else value.replace(tzinfo=TZ)
        if self.calendar_dates is None:
            return "UNKNOWN_CALENDAR"
        return phase_at(local) if local.date().isoformat() in self.calendar_dates else "HOLIDAY"

    def status(self, trade_date: str, event_id: str) -> str:
        if self._loaded_date != trade_date:
            self._load(trade_date)
        return self._states.get(event_id, {}).get("status", "PENDING")

    def tick(self, now: datetime) -> list[dict[str, Any]]:
        local = now.astimezone(TZ) if now.tzinfo else now.replace(tzinfo=TZ)
        trade_date = local.date().isoformat()
        actions: list[dict[str, Any]] = []
        if self.calendar_dates is None:
            self.degraded = True
            actions.append({"status": "CALENDAR_UNAVAILABLE", "at": local.isoformat()})
            return actions
        if trade_date not in self.calendar_dates:
            return [{"status": "HOLIDAY", "at": local.isoformat()}]
        self._load(trade_date)
        for event in self.events:
            status = self._states.get(event.event_id, {}).get("status", "PENDING")
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
        started = time.monotonic()
        try:
            result = _invoke_with_timeout(handler, (event, now, self.context, degraded), event.timeout_seconds)
            row.update(status="SUCCEEDED_DEGRADED" if degraded else "SUCCEEDED",
                        completed_at=now.isoformat(), duration_seconds=round(time.monotonic() - started, 3),
                        result=str(result)[:500])
            row.pop("next_attempt_at", None)
            actions.append({"event": event.event_id, "status": row["status"], "duration_seconds": row["duration_seconds"]})
        except Exception as exc:
            self._failed(event, now, row, exc, actions)

    def _failed(self, event: EventSpec, now: datetime, row: dict[str, Any], exc: Exception,
                actions: list[dict[str, Any]]) -> None:
        attempts = int(row.get("attempts", 0))
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
        try:
            payload = json.loads(self._state_path(trade_date).read_text(encoding="utf-8"))
            if payload.get("trade_date") == trade_date:
                self._states = payload.get("events", {})
        except (OSError, ValueError, AttributeError):
            pass

    def _save(self, trade_date: str, now: datetime) -> None:
        path = self._state_path(trade_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        heartbeat = {"trade_date": trade_date, "last_tick_at": now.isoformat(),
                     "phase": self.phase(now), "next_event": self._next_event(now),
                     "health": "DEGRADED" if self.degraded else "READY", "events": self._states}
        _atomic_json(path, {"trade_date": trade_date, "events": self._states, "heartbeat": heartbeat})
        _atomic_json(self.state_root / "heartbeat.json", heartbeat)
        self._dirty = False

    def heartbeat(self, now: datetime) -> dict[str, Any]:
        local = now.astimezone(TZ) if now.tzinfo else now.replace(tzinfo=TZ)
        trade_date = local.date().isoformat()
        if self._loaded_date != trade_date:
            self._load(trade_date)
        return {"trade_date": trade_date, "last_tick_at": local.isoformat(),
                "phase": self.phase(local), "next_event": self._next_event(local),
                "health": "DEGRADED" if self.degraded else "READY", "events": self._states}

    def write_heartbeat(self, now: datetime) -> None:
        """Persist a heartbeat independently of event-state changes."""
        payload = self.heartbeat(now)
        _atomic_json(self.state_root / "heartbeat.json", payload)

    def _next_event(self, now: datetime) -> str | None:
        for event in self.events:
            if self._states.get(event.event_id, {}).get("status") not in {"SUCCEEDED", "SUCCEEDED_DEGRADED", "MISSED", "CIRCUIT_OPEN"}:
                if datetime.combine(now.date(), _clock(event.start), TZ) >= now:
                    return event.event_id
        return None


def _invoke_with_timeout(handler: Callable[..., Any], args: tuple[Any, ...], timeout: int) -> Any:
    """Run injected work in a daemon thread and surface timeout as an event failure."""
    done = threading.Event()
    box: dict[str, Any] = {}
    def target():
        try:
            box["result"] = handler(*args)
        except BaseException as exc:  # propagate worker failures to the scheduler
            box["error"] = exc
        finally:
            done.set()
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    if not done.wait(timeout):
        raise TimeoutError(f"handler_timeout:{timeout}s")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)

