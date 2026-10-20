"""Late-start planning: what to do about each overdue dataset given the actual time."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .registry import load_registry, stale_items


def _hms(value: str) -> tuple[int, int, int]:
    parts = [int(p) for p in value.split(":")] + [0, 0]
    return parts[0], parts[1], parts[2]


def _clock(now: datetime) -> tuple[int, int, int]:
    return now.hour, now.minute, now.second


def reconcile_plan(now: datetime, catalog: str | Mapping[str, Any], *, trade_date: str,
                   prev_trade_date: str | None, registry: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Ordered actions for items not OK on a trading day.

    FETCH_NOW  refetchable/rebuildable and its source is ready (dependencies first);
    WAIT       source not ready yet (``ready_after`` in the future) or a dependency is still pending;
    GAP        a time-point capture that cannot be backfilled: record the gap, do not fabricate;
    NO_POLICY  overdue but the registry has no late_policy (needs a decision, never guessed).
    """
    items = load_registry() if registry is None else registry
    by_id = {item["id"]: item for item in items}
    states = {row["id"]: row for row in stale_items(now, catalog, items, trade_date=trade_date,
                                                    prev_trade_date=prev_trade_date)}
    todo = {i for i, row in states.items() if row["status"] in {"OVERDUE", "MISSING"}}
    plan: list[dict[str, Any]] = []
    done: set[str] = set()

    def visit(item_id: str, stack: tuple[str, ...] = ()) -> None:
        if item_id in done or item_id in stack:
            return
        policy = (by_id[item_id].get("late_policy") or {})
        for dep in policy.get("depends_on", []):
            if dep in todo:
                visit(dep, stack + (item_id,))
        done.add(item_id)
        if not policy:
            plan.append({"id": item_id, "action": "NO_POLICY"})
            return
        if policy["mode"] == "gap_only":
            until = policy.get("valid_until")
            plan.append({"id": item_id, "action": "GAP", "valid_until": until})
            return
        if _clock(now) < _hms(policy["ready_after"]):
            plan.append({"id": item_id, "action": "WAIT", "ready_after": policy["ready_after"]})
            return
        blocked = [dep for dep in policy.get("depends_on", [])
                   if dep in todo and next(r["action"] for r in plan if r["id"] == dep) != "FETCH_NOW"]
        if blocked:
            plan.append({"id": item_id, "action": "WAIT", "blocked_by": blocked})
            return
        plan.append({"id": item_id, "action": "FETCH_NOW", "event": by_id[item_id].get("guardian_event")})

    for item_id in [i["id"] for i in items if i["id"] in todo]:
        visit(item_id)
    return plan
