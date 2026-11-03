"""Validation of a timeline document (design 28, P2.3). Reuses what ``management`` already enforces on the flat
schedule (handler, gate, field and phase names, catch-up values) and adds the cross-reference and time rules that
nothing checked: dangling references, deadlines before their start, dependencies that run later, empty ladders.
"""
from __future__ import annotations

from typing import Any, Mapping

from .management import GATES, GO_V3_EVENT_FIELDS, GO_V3_HANDLERS
from .timeline import expand


def _clock_ok(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split(":")
    return len(parts) in (2, 3) and all(part.isdigit() for part in parts) and int(parts[0]) < 24 and int(parts[1]) < 60


def _phase_at(phases: list[Mapping[str, Any]], clock: str) -> str | None:
    for phase in phases:
        if phase["start"] <= clock[:5] < phase["end"]:
            return phase["name"]
    return None


def validate_timeline(doc: Mapping[str, Any]) -> list[str]:
    """Problems as ``<where>:<code>``; empty means the document may be turned into a schedule."""
    problems: list[str] = []
    functions, policies = doc.get("functions", {}), doc.get("policies", {})
    points = [(point["at"], item) for point in doc.get("timeline", []) for item in point["run"]]
    items = points + [(None, item) for item in doc.get("startup", [])]
    ids = [item["id"] for _at, item in items]
    for duplicate in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"{duplicate}:duplicate_id")
    if sorted(ids) != sorted(doc.get("order", [])):
        problems.append("order:does_not_list_each_trigger_once")
    for at, item in items:
        where = item["id"]
        if item["fn"] not in functions:
            problems.append(f"{where}:unknown_function:{item['fn']}")
            continue
        policy = item.get("policy", functions[item["fn"]].get("policy"))
        if policy is not None and policy not in policies:
            problems.append(f"{where}:unknown_policy:{policy}")
        if at is not None and not _clock_ok(at):
            problems.append(f"{where}:bad_clock:at")
    for name, tiers in ((name, policy.get("ladder")) for name, policy in policies.items()):
        names = [tier.get("name") for tier in tiers or []]
        if not names:
            problems.append(f"policy {name}:empty_ladder")
        if len(set(names)) != len(names):
            problems.append(f"policy {name}:duplicate_tier_name")
        for tier in tiers or []:
            if int(tier.get("attempts", 0)) < 1:
                problems.append(f"policy {name}:{tier.get('name')}:attempts_below_1")
            if "args" in tier and not isinstance(tier["args"], Mapping):
                problems.append(f"policy {name}:{tier.get('name')}:args_not_a_mapping")
    if any(problem.split(":")[1] in {"unknown_function", "bad_clock", "duplicate_id"} for problem in problems):
        return problems                       # the flat schedule cannot be built; the checks below need it
    schedule = expand(doc)
    events = {event["event_id"]: event for event in schedule["events"]}
    phases = schedule["global"].get("phases", [])
    for event in schedule["events"]:
        where = event["event_id"]
        unknown = set(event) - GO_V3_EVENT_FIELDS
        if unknown:
            problems.append(f"{where}:unknown_field:{','.join(sorted(unknown))}")
        if event.get("handler") not in GO_V3_HANDLERS:
            problems.append(f"{where}:unknown_handler:{event.get('handler')}")
        if event.get("gate") is not None and event["gate"] not in GATES:
            problems.append(f"{where}:unknown_gate:{event.get('gate')}")
        for field in ("deadline", "available_after", "late_until"):
            if event.get(field) not in (None, "") and not _clock_ok(event[field]):
                problems.append(f"{where}:bad_clock:{field}")
        start, deadline = event.get("at"), event.get("deadline")
        if start and deadline and _clock_ok(start) and _clock_ok(deadline) and deadline < start:
            problems.append(f"{where}:deadline_before_start")
        if start and event.get("available_after") and event["available_after"] > start:
            problems.append(f"{where}:available_after_later_than_start")
        if deadline and event.get("late_until") and event["late_until"] < deadline:
            problems.append(f"{where}:late_until_before_deadline")
        allowed = event.get("phases")
        if start and allowed and "any" not in allowed and _phase_at(phases, start) not in allowed:
            problems.append(f"{where}:start_in_a_phase_the_event_does_not_allow:{_phase_at(phases, start)}")
        for key in ("depends_on", "after"):
            for dep in event.get(key) or []:
                if dep not in events:
                    problems.append(f"{where}:{key}_unknown_event:{dep}")
                elif start and events[dep].get("at") and events[dep]["at"] > start:
                    problems.append(f"{where}:{key}_runs_later:{dep}")
    return problems
