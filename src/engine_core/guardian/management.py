"""Local versioned schedule and append-only guardian control records."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN
from pathlib import Path
from typing import Any

from .scheduler import EventSpec, TZ, _atomic_json

SCHEMA_VERSION = 2
GO_EVENT_FIELDS = frozenset({
    "event_id", "enabled", "calendar", "trigger_type", "start", "deadline",
    "order", "after", "gate", "handler", "timeout_seconds", "max_attempts",
    "retry_backoff", "retry_until_late", "silent_until_last", "catchup_mode",
    "on_deadline", "late_until", "cost", "phases", "on_phase_mismatch",
    "on_start", "data_date_offset", "available_after", "depends_on", "args",
})
HANDLERS = {"auction_report", "auction_watch", "baostock_probe", "boot_apply_plan",
            "boot_data_health", "daily_kline_backfill", "daily_kline_close", "boot_reconcile", "boot_trade_day", "calendar_refresh",
            "close_capture", "close_report", "fetch_item", "intraday_data", "kaipan_fetch",
            "kaipan_hot_capture", "night_report", "open_report", "postmarket_data",
            "premarket_data", "snapshot_check"}
GO_V3_HANDLERS = HANDLERS | {"selftest"}
GO_V3_TOP_FIELDS = frozenset({"schema_version", "revision", "timezone", "global", "profiles", "events"})
GO_V3_GLOBAL_FIELDS = frozenset({"heavy_max_load1", "heavy_max_load5",
                                "heavy_load_defer_max_polls", "heavy_cutoffs", "phases"})
GO_V3_PROFILE_FIELDS = frozenset({"catchup", "retry_backoff", "retry_until_late",
                                  "silent_until_last", "ladder", "window"})
GO_V3_WINDOW_FIELDS = frozenset({"deadline", "late_until"})
GO_V3_TIER_FIELDS = frozenset({"name", "args", "attempts", "backoff", "backoff_cap",
                                "timeout", "until", "budget_enabled",
                                "on_timeout", "alert_on_fallback"})
GO_V3_EVENT_FIELDS = frozenset({
    "id", "event_id", "enabled", "calendar", "trigger_type", "at", "deadline",
    "late_until", "window", "profile", "catchup", "ladder", "gate", "handler",
    "timeout_seconds", "max_attempts", "retry_backoff", "retry_until_late",
    "silent_until_last", "catchup_mode", "on_deadline", "order", "after", "cost",
    "phases", "on_phase_mismatch", "on_start", "data_date_offset", "available_after",
    "late_until_date_offset_days", "late_until_date_offset_error_codes",
    "late_until_clock_by_error_code", "depends_on", "args",
})
GO_V3_PHASE_FIELDS = frozenset({"name", "start", "end"})
_V3_DEFAULT_PHASES = (
    ("NIGHT", "00:00", "08:00"), ("PRE_MARKET", "08:00", "09:15"),
    ("AUCTION", "09:15", "09:25"), ("AUCTION_END", "09:25", "09:30"),
    ("OPEN", "09:30", "09:45"), ("INTRADAY_AM", "09:45", "11:30"),
    ("MIDDAY_BREAK", "11:30", "13:00"), ("PM_OPEN", "13:00", "13:15"),
    ("INTRADAY_PM", "13:15", "14:57"), ("CLOSE_AUCTION", "14:57", "15:00"),
    ("CLOSE", "15:00", "15:30"), ("POST_MARKET", "15:30", "24:00"),
)
_V3_ID = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")
_V3_TIME = re.compile(r"^\d{2}:\d{2}(?::\d{2})?$")
_V3_DURATION_PART = re.compile(r"(\d+(?:\.\d*)?|\.\d+)(ns|us|µs|ms|s|m|h)")
_V3_TEMPLATE = re.compile(r"\{([^{}]+)\}")
_V3_TEMPLATE_VARS = frozenset({"trade_date", "data_date", "asof", "event_id", "instance",
                               "attempt", "run_id", "sandbox_root", "python"})


def _schedule_gates() -> set[str]:
    try:
        payload = json.loads(Path(__file__).with_name("schedule.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {event["gate"] for event in payload.get("events", [])
            if isinstance(event, dict) and isinstance(event.get("gate"), str)}


GATES = _schedule_gates() | {"daily_kline_ready"}


def schedule_payload(revision: int = 1) -> dict[str, Any]:
    payload = json.loads(Path(__file__).with_name("schedule.json").read_text(encoding="utf-8"))
    payload["revision"] = revision
    return payload


def _validate_schedule_v2(payload: dict[str, Any], previous: dict[str, Any] | None = None) -> tuple[EventSpec, ...]:
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("timezone") != "Asia/Shanghai":
        raise ValueError("schedule_schema_or_timezone_invalid")
    if not isinstance(payload.get("revision"), int) or payload["revision"] < 1:
        raise ValueError("schedule_revision_invalid")
    global_config = payload.get("global")
    if not isinstance(global_config, dict) or not isinstance(global_config.get("phases"), list):
        raise ValueError("schedule_global_invalid")
    try:
        phase_names = {phase["name"] for phase in global_config["phases"]}
    except (KeyError, TypeError):
        raise ValueError("schedule_global_invalid") from None
    raw_events = payload.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        raise ValueError("schedule_events_invalid")
    events = []
    ids = set()
    for raw in raw_events:
        if not isinstance(raw, dict):
            raise ValueError("schedule_event_invalid")
        unknown = set(raw) - GO_EVENT_FIELDS
        if unknown:
            raise ValueError("schedule_event_field_unknown:" + ",".join(sorted(unknown)))
        if raw.get("catchup_mode", "skip") not in ("skip", "run_in_window"):
            raise ValueError("schedule_catchup_mode_invalid")
        if raw.get("on_deadline", "missed") not in ("missed", "degrade", "kill_missed"):
            raise ValueError("schedule_on_deadline_invalid")
        raw_phases = raw.get("phases")
        if raw_phases is not None and (not isinstance(raw_phases, list) or
                                       any(not isinstance(value, str) or
                                           (value != "any" and value not in phase_names)
                                           for value in raw_phases)):
            raise ValueError("schedule_phases_invalid")
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


def _v3_object(value: Any, path: str, allowed: frozenset[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("schedule_v3_type_invalid:" + path)
    unknown = set(value) - allowed
    if unknown:
        raise ValueError("schedule_v3_field_unknown:" + path + ":" + ",".join(sorted(unknown)))
    return value


def _v3_string(value: Any, path: str) -> str:
    if value is None:
        return ""  # encoding/json leaves non-pointer scalar fields at their zero value for null.
    if not isinstance(value, str):
        raise ValueError("schedule_v3_type_invalid:" + path)
    return value


def _v3_int(value: Any, path: str) -> int:
    if value is None:
        return 0
    if type(value) is not int:
        raise ValueError("schedule_v3_type_invalid:" + path)
    return value


def _v3_bool(value: Any, path: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError("schedule_v3_type_invalid:" + path)
    return value


def _v3_string_list(value: Any, path: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("schedule_v3_type_invalid:" + path)
    return [_v3_string(item, f"{path}[{index}]") for index, item in enumerate(value)]


def _v3_mapping(value: Any, path: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("schedule_v3_type_invalid:" + path)
    return value


def _v3_flatten_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _v3_flatten_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _v3_flatten_strings(item)


def _v3_time(value: str, path: str, *, allow_24: bool = False) -> tuple[str, int]:
    if allow_24 and value == "24:00":
        return value, 86400
    if not _V3_TIME.fullmatch(value):
        raise ValueError("schedule_v3_time_invalid:" + path)
    fmt = "%H:%M:%S" if len(value) == 8 else "%H:%M"
    try:
        parsed = datetime.strptime(value, fmt).time()
    except ValueError as exc:
        raise ValueError("schedule_v3_time_invalid:" + path) from exc
    normalized = parsed.strftime("%H:%M:%S")
    return normalized, parsed.hour * 3600 + parsed.minute * 60 + parsed.second


def _v3_duration(value: str) -> Decimal | None:
    if value in {"0", "+0", "-0"}:
        return Decimal(0)
    sign = Decimal(1)
    if value.startswith(("+", "-")):
        if value[0] == "-":
            sign = Decimal(-1)
        value = value[1:]
    factors_ns = {"ns": Decimal(1), "us": Decimal(1000), "µs": Decimal(1000),
                  "ms": Decimal(1_000_000), "s": Decimal(1_000_000_000),
                  "m": Decimal(60_000_000_000), "h": Decimal(3_600_000_000_000)}
    pos, total_ns = 0, Decimal(0)
    while pos < len(value):
        match = _V3_DURATION_PART.match(value, pos)
        if not match:
            return None
        try:
            # Go's time.ParseDuration truncates each component to nanoseconds.
            total_ns += (Decimal(match.group(1)) * factors_ns[match.group(2)]).to_integral_value(rounding=ROUND_DOWN)
        except InvalidOperation:
            return None
        pos = match.end()
    return sign * total_ns / Decimal(1_000_000_000) if pos else None


def _v3_backoff(value: str) -> bool:
    match = re.fullmatch(r"(.+)\*([0-9]+(?:\.[0-9]+)?)", value)
    if not match:
        return False
    base = _v3_duration(match.group(1))
    try:
        factor = Decimal(match.group(2))
    except InvalidOperation:
        return False
    return base is not None and Decimal(0) < base <= Decimal(86400) and Decimal(1) <= factor <= Decimal(1000)


def _v3_window(value: Any, path: str, *, check_order: bool = True) -> dict[str, str]:
    raw = _v3_object(value, path, GO_V3_WINDOW_FIELDS)
    result = {}
    for field in ("deadline", "late_until"):
        text = _v3_string(raw.get(field), path + "." + field)
        result[field] = _v3_time(text, path + "." + field)[0] if text else ""
    if (check_order and result["deadline"] and result["late_until"] and
            result["late_until"] < result["deadline"]):
        raise ValueError("schedule_v3_late_until_order:" + path)
    return result


def _v3_tiers(value: Any, scope: str, late_until: str) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("schedule_v3_type_invalid:" + scope)
    if not value:
        raise ValueError("schedule_ladder_empty:" + scope)
    names = set()
    tiers = []
    for index, raw in enumerate(value):
        path = f"{scope}:{index}"
        tier = _v3_object(raw, path, GO_V3_TIER_FIELDS)
        name = _v3_string(tier.get("name"), path + ".name")
        if not _V3_ID.fullmatch(name) or name in names:
            raise ValueError("schedule_ladder_name_invalid:" + path)
        names.add(name)
        attempts = _v3_int(tier.get("attempts"), path + ".attempts")
        if attempts < 1:
            raise ValueError("schedule_ladder_attempts_invalid:" + path)
        timeout = _v3_string(tier.get("timeout"), path + ".timeout")
        if not timeout:
            raise ValueError("schedule_ladder_timeout_required:" + path)
        timeout_seconds = _v3_duration(timeout)
        if timeout_seconds is None or timeout_seconds <= 0 or timeout_seconds > Decimal(86400):
            raise ValueError("schedule_ladder_timeout_invalid:" + path)
        backoff = _v3_string(tier.get("backoff"), path + ".backoff")
        if attempts > 1 and not backoff:
            raise ValueError("schedule_ladder_backoff_required:" + path)
        if backoff and not _v3_backoff(backoff):
            raise ValueError("schedule_ladder_backoff_invalid:" + path)
        cap = _v3_string(tier.get("backoff_cap"), path + ".backoff_cap")
        if cap:
            cap_seconds = _v3_duration(cap)
            if cap_seconds is None or cap_seconds <= 0 or cap_seconds > Decimal(86400):
                raise ValueError("schedule_ladder_backoff_cap_invalid:" + path)
        until = _v3_string(tier.get("until"), path + ".until")
        if until and until != "late_until":
            if len(until) != 8:
                raise ValueError("schedule_ladder_until_invalid:" + path)
            _v3_time(until, path + ".until")
        if until == "late_until" and not late_until:
            raise ValueError("schedule_ladder_late_until_required:" + path)
        _v3_mapping(tier.get("args"), path + ".args")
        _v3_bool(tier.get("budget_enabled"), path + ".budget_enabled")
        on_timeout = _v3_string(tier.get("on_timeout"), path + ".on_timeout")
        if on_timeout not in {"", "retry", "advance"}:
            raise ValueError("schedule_ladder_on_timeout_invalid:" + path)
        _v3_bool(tier.get("alert_on_fallback"), path + ".alert_on_fallback")
        tiers.append({**tier, "name": name, "attempts": attempts, "timeout": timeout,
                      "backoff": backoff, "backoff_cap": cap, "until": until})
    return tiers


def _v3_validate_global(value: Any) -> tuple[set[str], Any]:
    global_config = _v3_object(value, "global", GO_V3_GLOBAL_FIELDS)
    if "heavy_cutoffs" in global_config and global_config["heavy_cutoffs"] is not None:
        cutoffs = global_config["heavy_cutoffs"]
        if not isinstance(cutoffs, dict) or any(not isinstance(k, str) or
                                               (v is not None and not isinstance(v, str))
                                               for k, v in cutoffs.items()):
            raise ValueError("schedule_v3_type_invalid:global.heavy_cutoffs")
    for key in ("heavy_max_load1", "heavy_max_load5"):
        heavy = global_config.get(key)
        if key not in global_config:
            continue
        if isinstance(heavy, bool) or not (isinstance(heavy, (int, float)) or heavy is None or
                                           isinstance(heavy, str) and heavy == "cpus"):
            raise ValueError("schedule_v3_load_threshold_invalid")
        if isinstance(heavy, (int, float)):
            try:
                finite = math.isfinite(float(heavy))
            except OverflowError:
                finite = False
            if not finite or heavy < 0:
                raise ValueError("schedule_v3_load_threshold_invalid")
        if heavy is None:
            raise ValueError("schedule_v3_load_threshold_invalid")
    defer_polls = global_config.get("heavy_load_defer_max_polls")
    if "heavy_load_defer_max_polls" in global_config and (
            isinstance(defer_polls, bool) or not isinstance(defer_polls, int) or defer_polls < 1):
        raise ValueError("schedule_v3_load_defer_polls_invalid")
    phases_value = global_config.get("phases")
    if phases_value is None:
        return {name for name, _, _ in _V3_DEFAULT_PHASES}, global_config
    if not isinstance(phases_value, list):
        raise ValueError("schedule_v3_type_invalid:global.phases")
    names = set()
    previous_end = 0
    for index, phase_value in enumerate(phases_value):
        path = f"global.phases[{index}]"
        phase = _v3_object(phase_value, path, GO_V3_PHASE_FIELDS)
        name = _v3_string(phase.get("name"), path + ".name")
        start_text = _v3_string(phase.get("start"), path + ".start")
        end_text = _v3_string(phase.get("end"), path + ".end")
        if not name or name in names:
            raise ValueError("schedule_v3_phase_name_invalid:" + path)
        names.add(name)
        try:
            if len(start_text) != 5 or (end_text != "24:00" and len(end_text) != 5):
                raise ValueError("phase time must use HH:MM")
            _, start = _v3_time(start_text, path + ".start")
            _, end = _v3_time(end_text, path + ".end", allow_24=True)
        except ValueError:
            raise ValueError("schedule_v3_phase_time_invalid:" + path) from None
        if start >= end:
            raise ValueError("schedule_v3_phase_order:" + path)
        if index == 0 and start != 0 or index > 0 and start != previous_end:
            raise ValueError("schedule_v3_phase_coverage:" + path)
        previous_end = end
    if phases_value and previous_end != 86400:
        raise ValueError("schedule_v3_phase_coverage:global.phases")
    return names, global_config


def _v3_validate_schedule(payload: dict[str, Any], previous: dict[str, Any] | None = None) -> tuple[EventSpec, ...]:
    if not isinstance(payload, dict):
        raise ValueError("schedule_v3_type_invalid:root")
    unknown = set(payload) - GO_V3_TOP_FIELDS
    if unknown:
        raise ValueError("schedule_v3_field_unknown:root:" + ",".join(sorted(unknown)))
    version = payload.get("schema_version")
    timezone = _v3_string(payload.get("timezone"), "timezone")
    if type(version) is not int or version != 3 or timezone != "Asia/Shanghai":
        raise ValueError("schedule_v3_schema_or_timezone_invalid")
    if "revision" in payload:
        _v3_int(payload["revision"], "revision")
    phase_names, _ = _v3_validate_global(payload.get("global"))

    profiles_value = payload.get("profiles")
    if profiles_value is None:
        profiles_value = {}
    if not isinstance(profiles_value, dict):
        raise ValueError("schedule_v3_type_invalid:profiles")
    profiles: dict[str, dict[str, Any]] = {}
    for name in sorted(profiles_value):
        path = "profiles." + str(name)
        raw = _v3_object(profiles_value[name], path, GO_V3_PROFILE_FIELDS)
        catchup = _v3_string(raw.get("catchup"), path + ".catchup")
        if catchup and catchup not in {"skip", "run_in_window"}:
            raise ValueError("schedule_v3_profile_catchup_invalid:" + str(name))
        retry_backoff = _v3_string(raw.get("retry_backoff"), path + ".retry_backoff")
        _v3_bool(raw.get("retry_until_late"), path + ".retry_until_late")
        _v3_bool(raw.get("silent_until_last"), path + ".silent_until_last")
        window = _v3_window(raw.get("window"), path + ".window")
        ladder = _v3_tiers(raw.get("ladder"), "profile:" + str(name), "23:59:59")
        profiles[str(name)] = {"catchup": catchup, "retry_backoff": retry_backoff,
                               "retry_until_late": _v3_bool(raw.get("retry_until_late"), path + ".retry_until_late"),
                               "silent_until_last": _v3_bool(raw.get("silent_until_last"), path + ".silent_until_last"),
                               "window": window, "ladder": ladder}

    raw_events = payload.get("events")
    if raw_events is None:
        raw_events = []
    if not isinstance(raw_events, list):
        raise ValueError("schedule_v3_type_invalid:events")
    compiled: list[EventSpec] = []
    seen_ids: set[str] = set()
    graph: dict[str, set[str]] = {}
    handler_supports_degrade: dict[str, bool] = {}
    deadline_events: list[tuple[str, str, str]] = []
    for index, source in enumerate(raw_events):
        path = f"events[{index}]"
        event = _v3_object(source, path, GO_V3_EVENT_FIELDS)
        event_id = _v3_string(event.get("id"), path + ".id")
        alternate_id = _v3_string(event.get("event_id"), path + ".event_id")
        if event_id and alternate_id:
            raise ValueError("schedule_v3_event_id_ambiguous:" + path)
        event_id = event_id or alternate_id
        handler = _v3_string(event.get("handler"), path + ".handler")
        if handler not in GO_V3_HANDLERS:
            raise ValueError("schedule_handler_unknown:" + handler)
        profile_name = _v3_string(event.get("profile"), path + ".profile")
        if profile_name and profile_name not in profiles:
            raise ValueError("schedule_profile_unknown:" + profile_name)
        profile = profiles.get(profile_name, {})
        raw_at = event.get("at")
        trigger_type = _v3_string(event.get("trigger_type"), path + ".trigger_type")
        if not trigger_type:
            trigger_type = "window" if raw_at is not None else "startup"
        if trigger_type not in {"window", "startup"}:
            raise ValueError("schedule_v3_trigger_type_invalid:" + path)
        at_list = isinstance(raw_at, list)
        if raw_at is None:
            at_values = []
        elif isinstance(raw_at, str):
            at_values = [raw_at]
        elif isinstance(raw_at, list):
            if not raw_at:
                raise ValueError("schedule_v3_at_list_empty:" + event_id)
            at_values = [_v3_string(value, f"{path}.at[{i}]") for i, value in enumerate(raw_at)]
        else:
            raise ValueError("schedule_v3_at_type_invalid:" + event_id)
        normalized_at = []
        for i, value in enumerate(at_values):
            try:
                normalized_at.append(_v3_time(value, f"{path}.at[{i}]")[0])
            except ValueError as exc:
                raise ValueError(f"schedule_at_invalid:{event_id}:{i}") from exc
        if len(set(normalized_at)) != len(normalized_at) or len({value[:5] for value in normalized_at}) != len(normalized_at):
            raise ValueError("schedule_v3_at_duplicate:" + event_id)

        profile_window = profile.get("window", {})
        event_window = _v3_window(event.get("window"), path + ".window", check_order=False)
        deadline = (_v3_string(event.get("deadline"), path + ".deadline") or
                    event_window.get("deadline") or profile_window.get("deadline", ""))
        late_until = (_v3_string(event.get("late_until"), path + ".late_until") or
                      event_window.get("late_until") or profile_window.get("late_until", ""))
        if deadline:
            deadline = _v3_time(deadline, path + ".deadline")[0]
        if late_until:
            late_until = _v3_time(late_until, path + ".late_until")[0]
        late_until_date_offset = _v3_int(event.get("late_until_date_offset_days"),
                                         path + ".late_until_date_offset_days")
        if late_until_date_offset not in {0, 1} or late_until_date_offset and not late_until:
            raise ValueError("schedule_v3_late_until_date_offset_invalid:" + event_id)
        late_until_date_offset_error_codes = _v3_string_list(
            event.get("late_until_date_offset_error_codes"), path + ".late_until_date_offset_error_codes")
        if (any(not code or code.strip() != code for code in late_until_date_offset_error_codes) or
                late_until_date_offset_error_codes and late_until_date_offset == 0):
            raise ValueError("schedule_v3_late_until_date_offset_error_codes_invalid:" + event_id)
        clock_overrides_raw = event.get("late_until_clock_by_error_code")
        if clock_overrides_raw is None:
            clock_overrides_raw = {}
        if not isinstance(clock_overrides_raw, dict):
            raise ValueError("schedule_v3_late_until_clock_by_error_code_invalid:" + event_id)
        clock_overrides = {}
        for code, value in clock_overrides_raw.items():
            if not isinstance(code, str) or not code or code.strip() != code:
                raise ValueError("schedule_v3_late_until_clock_by_error_code_invalid:" + event_id)
            clock_value = _v3_string(value, path + ".late_until_clock_by_error_code." + code)
            clock_overrides[code] = _v3_time(
                clock_value, path + ".late_until_clock_by_error_code." + code)[0]
        if (trigger_type == "window" and deadline and late_until and
                late_until_date_offset == 0 and late_until < deadline):
            raise ValueError("schedule_v3_late_until_order:" + event_id)
        if trigger_type == "window" and (not normalized_at or not deadline):
            raise ValueError("schedule_v3_window_required:" + event_id)

        catchup = profile.get("catchup", "")
        catchup_mode = _v3_string(event.get("catchup_mode"), path + ".catchup_mode") or catchup
        catchup_mode = _v3_string(event.get("catchup"), path + ".catchup") or catchup_mode or "skip"
        if catchup_mode not in {"skip", "run_in_window"}:
            raise ValueError("schedule_v3_catchup_invalid:" + event_id)
        on_deadline = _v3_string(event.get("on_deadline"), path + ".on_deadline")
        if on_deadline and on_deadline not in {"degrade", "kill_missed", "missed"}:
            raise ValueError("schedule_v3_on_deadline_invalid:" + event_id)
        phases = event.get("phases")
        if phases is not None:
            phases = _v3_string_list(phases, path + ".phases")
            if any(name != "any" and name not in phase_names for name in phases):
                raise ValueError("schedule_phases_invalid")
        event_retry_backoff = _v3_string(event.get("retry_backoff"), path + ".retry_backoff")
        retry_backoff = event_retry_backoff or profile.get("retry_backoff", "")
        if retry_backoff:
            retry_seconds = _v3_duration(retry_backoff)
            if retry_seconds is None or retry_seconds <= 0 or retry_seconds > Decimal(600):
                raise ValueError("schedule_v3_retry_backoff_invalid:" + event_id)
        retry_until_late = _v3_bool(event.get("retry_until_late"), path + ".retry_until_late") or profile.get("retry_until_late", False)
        silent_until_last = _v3_bool(event.get("silent_until_last"), path + ".silent_until_last") or profile.get("silent_until_last", False)

        event_ladder_value = event.get("ladder")
        event_ladder = _v3_tiers(event_ladder_value, "event:" + event_id, late_until) if event_ladder_value is not None else None
        ladder = event_ladder if event_ladder is not None else profile.get("ladder")
        timeout_value = _v3_int(event.get("timeout_seconds"), path + ".timeout_seconds")
        max_attempts_value = _v3_int(event.get("max_attempts"), path + ".max_attempts")
        if ladder:
            first = ladder[0]
            timeout_seconds_decimal = _v3_duration(first["timeout"])
            # Go permits sub-second ladder timeouts while EventSpec stores whole seconds.
            timeout_seconds = max(1, int(timeout_seconds_decimal.to_integral_value(rounding=ROUND_CEILING)))
            max_attempts = first["attempts"]
        else:
            if timeout_value < 1:
                raise ValueError("schedule_v3_legacy_timeout_required:" + event_id)
            max_attempts = max(1, max_attempts_value)
            timeout_seconds = timeout_value
        if max_attempts < 1 or timeout_seconds > 86400:
            raise ValueError("schedule_v3_bounds_invalid:" + event_id)
        if retry_until_late and not late_until:
            raise ValueError("schedule_v3_late_until_required:" + event_id)
        cost = _v3_string(event.get("cost"), path + ".cost")
        if cost not in ("", "light", "medium", "heavy"):
            raise ValueError("schedule_v3_cost_invalid:" + event_id)
        on_start = _v3_string(event.get("on_start"), path + ".on_start")
        if on_start and on_start != "reconcile":
            raise ValueError("schedule_v3_on_start_invalid:" + event_id)
        phase_mismatch = _v3_string(event.get("on_phase_mismatch"), path + ".on_phase_mismatch")
        if phase_mismatch and phase_mismatch not in {"defer", "skip"}:
            raise ValueError("schedule_v3_phase_mismatch_invalid:" + event_id)
        available_after = event.get("available_after")
        if available_after is not None and not isinstance(available_after, str):
            raise ValueError("schedule_v3_type_invalid:" + path + ".available_after")
        args = _v3_mapping(event.get("args"), path + ".args")
        if args is not None:
            for value in _v3_flatten_strings(args):
                for variable in _V3_TEMPLATE.findall(value):
                    if variable not in _V3_TEMPLATE_VARS:
                        raise ValueError("schedule_v3_template_variable_invalid:" + event_id + ":" + variable)
        depends_on = _v3_string_list(event.get("depends_on"), path + ".depends_on")
        after = _v3_string_list(event.get("after"), path + ".after")
        _v3_int(event.get("order"), path + ".order")
        _v3_int(event.get("data_date_offset"), path + ".data_date_offset")
        gate = _v3_string(event.get("gate"), path + ".gate")
        calendar = _v3_string(event.get("calendar"), path + ".calendar")
        enabled = _v3_bool(event.get("enabled"), path + ".enabled")
        handler_supports_degrade["legacy_" + handler] = trigger_type == "window"
        if not event_id or not _V3_ID.fullmatch(event_id):
            raise ValueError("schedule_v3_event_id_invalid:" + path)
        if event_id in seen_ids:
            raise ValueError("schedule_v3_duplicate_event_id:" + event_id)
        seen_ids.add(event_id)
        dependencies = set(depends_on) | set(after)
        graph[event_id] = dependencies
        deadline_events.append((event_id, "legacy_" + handler, on_deadline))

        retry_until_late_event = _v3_bool(event.get("retry_until_late"), path + ".retry_until_late") or profile.get("retry_until_late", False)
        if retry_until_late_event and not late_until:
            raise ValueError("schedule_v3_late_until_required:" + event_id)
        compiled.append(EventSpec(
            event_id=event_id, start=normalized_at[0] if normalized_at else "",
            deadline=deadline, gate=gate, handler=handler, max_attempts=max_attempts,
            timeout_seconds=timeout_seconds, depends_on=tuple(depends_on),
            available_after=available_after, data_date_offset=_v3_int(event.get("data_date_offset"), path + ".data_date_offset"),
            trigger_type=trigger_type, order=_v3_int(event.get("order"), path + ".order"),
            after=tuple(after), late_until=late_until or None,
            late_until_date_offset_days=late_until_date_offset,
            late_until_date_offset_error_codes=tuple(late_until_date_offset_error_codes),
            late_until_clock_by_error_code=clock_overrides,
            cost=cost or None,
            phases=tuple(phases) if phases is not None else None,
            on_phase_mismatch=phase_mismatch or None, on_start=on_start or None,
            retry_until_late=retry_until_late_event, retry_backoff=retry_backoff or None,
            silent_until_last=silent_until_last, on_deadline=on_deadline or "missed",
            catchup_mode=catchup_mode, enabled=enabled, calendar=calendar, args=args,
        ))

    for event_id, dependencies in graph.items():
        missing = dependencies - seen_ids
        if missing:
            raise ValueError("schedule_dependency_unknown")
    for event_id, handler_name, on_deadline in deadline_events:
        if on_deadline == "degrade" and not handler_supports_degrade.get(handler_name, False):
            raise ValueError("schedule_v3_degrade_unsupported:" + event_id)
    visiting, visited = set(), set()
    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError("schedule_dependency_cycle")
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)
    for event_id in graph:
        visit(event_id)

    if previous:
        old_events = previous.get("events", [])
        old = {}
        for source in old_events:
            if not isinstance(source, dict):
                continue
            old_id = source.get("event_id") or source.get("id")
            if previous.get("schema_version") == 2:
                old_start = source.get("start")
            else:
                at_value = source.get("at")
                at_value = at_value[0] if isinstance(at_value, list) and at_value else at_value
                try:
                    old_start = _v3_time(at_value, "previous.at")[0] if isinstance(at_value, str) else ""
                except ValueError:
                    old_start = ""
            old[old_id] = {"start": old_start, "handler": source.get("handler")}
        new = {event.event_id: event for event in compiled}
        for old_id, old_event in old.items():
            for new_id, new_event in new.items():
                if old_id != new_id and (old_event.get("start"), old_event.get("handler")) == (new_event.start, new_event.handler):
                    raise ValueError("schedule_alias_id_rejected")
    return tuple(compiled)


def validate_schedule(payload: dict[str, Any], previous: dict[str, Any] | None = None) -> tuple[EventSpec, ...]:
    if not isinstance(payload, dict):
        raise ValueError("schedule_schema_or_timezone_invalid")
    version = payload.get("schema_version")
    if version == 3:
        return _v3_validate_schedule(payload, previous)
    return _validate_schedule_v2(payload, previous)
