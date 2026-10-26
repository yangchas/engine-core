"""Late-start planning.

Principle (user, 2026-10-20): at start-up only *late* data is fetched. An item is late when
its trigger point (``late_policy.trigger_at``) has passed and its partition is still missing.
Before the trigger nothing is fetched. Light items run at once (parallel lane); heavy items
(e.g. daily bars) are deferred to an idle phase and never run during the trading session.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any, Mapping

from .registry import (REGISTRY_PATH, catalog_manifest, catalog_partition_present,
                       daily_kline_needs_upgrade, derived_daily_kline_input_stale,
                       load_registry)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


DAILY_KLINE_UPGRADE_MAX_ATTEMPTS = _env_int("CORE_DAILY_KLINE_UPGRADE_MAX_ATTEMPTS", 3)
DAILY_KLINE_UPGRADE_MIN_INTERVAL_SECONDS = _env_int(
    "CORE_DAILY_KLINE_UPGRADE_MIN_INTERVAL_SECONDS", 600, minimum=0)


def daily_kline_window_id(now: datetime, phase: str | None) -> str:
    """Return the actual heavy quiet window for this reconcile or fetch attempt."""
    rules = _rules()
    quiet = list(rules.get("quiet_phases_for_heavy", []))
    from .scheduler import _schedule_source, phase_at
    cutoff = _schedule_source()["global"]["heavy_cutoffs"]["PRE_MARKET"]
    clock = _clock(now)
    current = phase if phase in rules.get("phase_names", []) else phase_at(now)
    if current == "PRE_MARKET" and clock >= _hms(cutoff):
        current = "MIDDAY_BREAK"
    elif current not in quiet:
        order = list(rules.get("phase_names", []))
        try:
            index = order.index(current)
        except ValueError:
            index = -1
        future = [value for value in quiet if value in order and order.index(value) > index]
        if future:
            current = future[0]
        else:
            return (now.date() + timedelta(days=1)).isoformat() + ":PRE_MARKET"
    return f"{now.date().isoformat()}:{current}"


def daily_kline_next_retry_window(now: datetime, phase: str | None) -> str:
    current = daily_kline_window_id(now, phase)
    day, current_phase = current.split(":", 1)
    next_phase = {"NIGHT": "PRE_MARKET", "PRE_MARKET": "MIDDAY_BREAK",
                  "MIDDAY_BREAK": "CLOSE", "CLOSE": "POST_MARKET"}.get(current_phase)
    if next_phase:
        return f"{day}:{next_phase}"
    next_day = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    return f"{next_day}:PRE_MARKET"


def daily_kline_retry_availability(attempt: Any, now: datetime,
                                   phase: str | None,
                                   partition: str | None = None) -> tuple[bool, str]:
    """Check a per-partition quiet-window budget and the minimum interval."""
    if not isinstance(attempt, Mapping):
        return True, ""
    if (not attempt.get("window_id") and partition and
            attempt.get("status") in {"exhausted", "GAP_WINDOW_EXHAUSTED"} and
            now.date().isoformat() <= partition):
        return False, "window_budget_exhausted"
    if attempt.get("window_id") != daily_kline_window_id(now, phase):
        return True, ""
    if int(attempt.get("attempts", 0) or 0) >= DAILY_KLINE_UPGRADE_MAX_ATTEMPTS:
        return False, "window_budget_exhausted"
    last = attempt.get("last_attempt_at")
    if last:
        try:
            previous = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            current = now if now.tzinfo else now.astimezone()
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=current.tzinfo)
            if (current - previous).total_seconds() < DAILY_KLINE_UPGRADE_MIN_INTERVAL_SECONDS:
                return False, "minimum_interval"
        except (TypeError, ValueError, OverflowError):
            pass
    return True, ""


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
                   cpus: int | None = None,
                   upgrade_attempts: Mapping[str, Any] | None = None,
                   daily_kline_candidates: list[str] | None = None) -> list[dict[str, Any]]:
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
    from .scheduler import _schedule_source
    cutoff = _schedule_source()["global"]["heavy_cutoffs"]["PRE_MARKET"]
    by_id = {item["id"]: item for item in items}

    due: dict[str, tuple[str | None, dict[str, Any]]] = {}
    upgrade_versions: dict[str, str] = {}
    for item in items:
        dataset = item["artifact"].get("catalog_dataset")
        policy = item.get("late_policy")
        if not dataset or item.get("retired"):
            continue
        if not policy:
            if item["id"] == "auction_points_raw" or (item.get("freshness") and isinstance(item["freshness"], Mapping)):
                due.setdefault(item["id"], (None, {}))
            continue
        if item["id"] == "daily_kline" and daily_kline_candidates is not None:
            for candidate in sorted(set(daily_kline_candidates)):
                current_manifest = catalog_manifest(catalog, dataset, candidate)
                version = str((current_manifest or {}).get("version", ""))
                degraded = daily_kline_needs_upgrade(current_manifest)
                if catalog_partition_present(catalog, dataset, candidate, now) and not degraded:
                    continue
                attempt = (upgrade_attempts or {}).get(candidate)
                if attempt is None:
                    attempt = (upgrade_attempts or {}).get(f"{candidate}:{version}")
                available, _reason = daily_kline_retry_availability(attempt, now, phase, candidate)
                if not available:
                    break
                due[item["id"]] = (candidate, policy)
                if degraded and version:
                    upgrade_versions[item["id"]] = version
                break
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
        if item["id"] == "daily_kline":
            current_manifest = catalog_manifest(catalog, dataset, target)
            version = str((current_manifest or {}).get("version", ""))
            budget_key = target
            if daily_kline_needs_upgrade(current_manifest) and version:
                attempt = (upgrade_attempts or {}).get(budget_key)
                if attempt is None:
                    attempt = (upgrade_attempts or {}).get(f"{target}:{version}")
                available, _reason = daily_kline_retry_availability(attempt, now, phase, target)
                if available:
                    due[item["id"]] = (target, policy)
                    upgrade_versions[item["id"]] = version
                continue
        if (not catalog_partition_present(catalog, dataset, target, now)
                or derived_daily_kline_input_stale(catalog, dataset, target)):
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
        if item_id in upgrade_versions:
            entry.update({"upgrade_daily_kline": True, "upgrade_version": upgrade_versions[item_id],
                          "reason": "upgrade_degraded_daily_kline", "depends_on": []})
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
