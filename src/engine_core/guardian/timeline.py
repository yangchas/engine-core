"""Timeline view of the guardian schedule (design 28, P2.1): the same schedule, organised by function and time point.

``to_timeline`` folds the 96 flat events of ``schedule.json`` into
  functions  what runs (the fields every event of a family shares),
  policies   how failure is handled (the existing profiles, renamed),
  timeline   when it runs (time points, each listing the functions hung on it) and ``startup`` for boot-time events,
  order      the original event order, so the folding loses nothing.
``expand`` is the exact inverse. Nothing here reads or writes the live configuration.
"""
from __future__ import annotations

import copy
import re
from collections import defaultdict
from typing import Any, Mapping

FORMAT = "timeline/1"
_META = ("schema_version", "revision", "timezone")
_TRIGGER_KEYS = ("event_id", "at", "deadline")      # per-trigger by nature; everything else is shared when constant


def _seconds(clock: str) -> int:
    hours, minutes, seconds = (int(part) for part in clock.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def _clock(total: int) -> str:
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _is_full_clock(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"\d\d:\d\d:\d\d", value) is not None


def family_of(event_id: str) -> str:
    """``limit_up_today_091600`` -> ``limit_up_today``; ``fetch_item`` stays as it is."""
    return re.sub(r"[_@:]?\d.*$", "", event_id) or event_id


def _canon(value: Any) -> str:
    import json
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def to_timeline(schedule: Mapping[str, Any]) -> dict[str, Any]:
    events = [copy.deepcopy(dict(event)) for event in schedule["events"]]
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        families[family_of(event["event_id"])].append(event)
    functions: dict[str, dict[str, Any]] = {}
    shared_deadline: dict[str, int] = {}
    for name, members in families.items():
        keys = set().union(*(set(event) for event in members)) - set(_TRIGGER_KEYS)
        body = {key: members[0][key] for key in sorted(keys)
                if all(key in event and _canon(event[key]) == _canon(members[0][key]) for event in members)}
        if "profile" in body:
            body["policy"] = body.pop("profile")
        full = all(_is_full_clock(event.get("at")) and _is_full_clock(event.get("deadline")) for event in members)
        deltas = {_seconds(event["deadline"]) - _seconds(event["at"]) for event in members} if full else set()
        if len(members) > 1 and len(deltas) == 1:
            shared_deadline[name] = deltas.pop()
            body["deadline_after_seconds"] = shared_deadline[name]
        functions[name] = body
    timeline: dict[str, list[dict[str, Any]]] = defaultdict(list)
    startup: list[dict[str, Any]] = []
    for event in events:
        name = family_of(event["event_id"])
        body = functions[name]
        item: dict[str, Any] = {"fn": name, "id": event["event_id"]}
        for key, value in event.items():
            if key in _TRIGGER_KEYS:
                continue
            target = "policy" if key == "profile" else key
            if target not in body or _canon(body[target]) != _canon(value):
                item[target] = value
        if "deadline" in event and name not in shared_deadline:
            item["deadline"] = event["deadline"]
        elif "deadline" in event and "at" not in event:
            item["deadline"] = event["deadline"]
        if "at" in event:
            timeline[event["at"]].append(item)
        else:
            startup.append(item)
    return {
        "format": FORMAT,
        "meta": {key: schedule[key] for key in _META},
        "global": copy.deepcopy(schedule["global"]),
        "policies": copy.deepcopy(schedule["profiles"]),
        "functions": functions,
        "timeline": [{"at": at, "run": timeline[at]} for at in sorted(timeline)],
        "startup": startup,
        "order": [event["event_id"] for event in events],
    }


def expand(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Inverse of ``to_timeline``: the flat ``schedule.json`` structure."""
    by_id: dict[str, dict[str, Any]] = {}
    items = [(point["at"], item) for point in doc["timeline"] for item in point["run"]]
    items += [(None, item) for item in doc["startup"]]
    for at, item in items:
        body = copy.deepcopy(doc["functions"][item["fn"]])
        delta = body.pop("deadline_after_seconds", None)
        event: dict[str, Any] = dict(body)
        for key, value in item.items():
            if key not in ("fn", "id"):
                event[key] = copy.deepcopy(value)
        if "policy" in event:
            event["profile"] = event.pop("policy")
        event["event_id"] = item["id"]
        if at is not None:
            event["at"] = at
        if delta is not None and "deadline" not in event:
            event["deadline"] = _clock(_seconds(at) + delta)
        by_id[item["id"]] = event
    schedule: dict[str, Any] = dict(doc["meta"])
    schedule["global"] = copy.deepcopy(doc["global"])
    schedule["profiles"] = copy.deepcopy(doc["policies"])
    schedule["events"] = [by_id[event_id] for event_id in doc["order"]]
    return schedule
