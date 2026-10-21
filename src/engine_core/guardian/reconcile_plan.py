"""Late-start planning.

Principle (user, 2026-10-20): at start-up only *late* data is fetched. An item is late when
its trigger point (``late_policy.trigger_at``) has passed and its partition is still missing.
Before the trigger nothing is fetched. Light items run at once (parallel lane); heavy items
(e.g. daily bars) are deferred to an idle phase and never run during the trading session.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from .registry import REGISTRY_PATH, catalog_partition_present, load_registry


def _hms(value: str) -> tuple[int, int, int]:
    parts = [int(p) for p in str(value).split(":")] + [0, 0]
    return parts[0], parts[1], parts[2]


def _clock(now: datetime) -> tuple[int, int, int]:
    return now.hour, now.minute, now.second


def _rules() -> dict[str, Any]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def reconcile_plan(now: datetime, catalog: str | Mapping[str, Any], *, trade_date: str,
                   prev_trade_date: str | None, registry: list[dict[str, Any]] | None = None,
                   phase: str | None = None, load1: float | None = None,
                   cpus: int | None = None) -> list[dict[str, Any]]:
    """Ordered actions for late items on a trading day (dependencies first).

    FETCH_NOW  late and cheap enough to run now; ``lane`` is ``parallel`` (light) or ``serial``;
    DEFER      late but heavy: not during the session or under load; run in a quiet phase;
    GAP        late time-point capture that cannot be backfilled: record the gap, never fabricate;
    NO_POLICY  late catalog item without a late_policy (needs a decision, never guessed).
    Items whose trigger point is still ahead are not in the plan at all.
    """
    items = load_registry() if registry is None else registry
    if not isinstance(catalog, Mapping):
        from pathlib import Path
        catalog = json.loads(Path(catalog).read_text(encoding="utf-8"))
    rules = _rules()
    quiet = set(rules.get("quiet_phases_for_heavy", []))
    cutoff = rules.get("heavy_cutoffs", {}).get(
        "PRE_MARKET", rules.get("heavy_rules", {}).get("pre_market_heavy_cutoff", "09:10:00"))
    by_id = {item["id"]: item for item in items}

    due: dict[str, tuple[str | None, dict[str, Any]]] = {}
    for item in items:
        dataset = item["artifact"].get("catalog_dataset")
        policy = item.get("late_policy")
        if not dataset:
            continue
        if not policy:
            if item["id"] == "auction_points_raw" or (item.get("freshness") and isinstance(item["freshness"], Mapping)):
                due.setdefault(item["id"], (None, {}))
            continue
        triggered = _clock(now) >= _hms(policy["trigger_at"])
        target = trade_date if triggered else (prev_trade_date if policy.get("partition_before_trigger") == "D-1" else None)
        if target is None:
            continue  # trigger point still ahead: do not fetch
        rule = item["freshness"] if isinstance(item["freshness"], Mapping) else {}
        if rule.get("partition") == "D-1" and triggered:
            target = prev_trade_date
        if rule.get("partition") == "latest":
            keys = [k for k, v in catalog.get("datasets", {}).get(dataset, {}).get("dates", {}).items()
                    if catalog_partition_present(catalog, dataset, k, now)]
            if keys and (datetime.fromisoformat(trade_date) - datetime.fromisoformat(max(keys))).days <= rule.get("max_age_days", 8):
                continue
            target = trade_date
        if not catalog_partition_present(catalog, dataset, target, now):
            due[item["id"]] = (target, policy)

    plan: list[dict[str, Any]] = []
    done: set[str] = set()

    def visit(item_id: str, stack: tuple[str, ...] = ()) -> None:
        if item_id in done or item_id in stack:
            return
        target, policy = due[item_id]
        for dep in (policy or {}).get("depends_on", []):
            if dep in due:
                visit(dep, stack + (item_id,))
        done.add(item_id)
        if not policy:
            plan.append({"id": item_id, "action": "NO_POLICY"})
            return
        if policy["mode"] == "manual":
            plan.append({"id": item_id, "action": "NEEDS_OPERATOR", "partition": target,
                         "reason": "manual_reference_import_required"})
            return
        if policy["mode"] == "gap_only":
            plan.append({"id": item_id, "action": "GAP", "partition": target, "valid_until": policy.get("valid_until")})
            return
        if not by_id[item_id].get("fetch_job"):
            plan.append({"id": item_id, "partition": target, "action": "NO_FETCH_JOB",
                         "reason": "independent_fetch_job_unconfigured"})
            return
        blocked = [dep for dep in policy.get("depends_on", [])
                   if dep in due and next(r["action"] for r in plan if r["id"] == dep) not in {"FETCH_NOW"}]
        cost = (by_id[item_id].get("cost") or {}).get("class", "medium")
        entry = {"id": item_id, "partition": target, "cost": cost, "event": by_id[item_id].get("guardian_event"),
                 "fetch_job": by_id[item_id].get("fetch_job"), "phase": phase,
                 "depends_on": list(policy.get("depends_on", []))}
        if blocked:
            plan.append({**entry, "action": "DEFER", "reason": "blocked_by:" + ",".join(blocked)})
            return
        if cost == "heavy":
            in_quiet = phase in quiet and not (phase == "PRE_MARKET" and _clock(now) >= _hms(cutoff))
            if phase is not None and not in_quiet:
                plan.append({**entry, "action": "DEFER", "reason": "session_or_busy_phase:" + str(phase),
                             "run_at": "next quiet phase (" + "/".join(sorted(quiet)) + ")"})
                return
            if load1 is not None and cpus and load1 > cpus:
                plan.append({**entry, "action": "DEFER", "reason": f"load1={load1}>cpus={cpus}"})
                return
        plan.append({**entry, "action": "FETCH_NOW", "lane": "parallel" if cost == "light" else "serial"})

    for item_id in [i["id"] for i in items if i["id"] in due]:
        visit(item_id)
    return plan
