"""Dataset chain: judge which derived partitions are missing or stale and rebuild them in dependency order.

A chain is declared in guardian/chains.json (``nodes``: dataset -> the datasets it requires; ``external``: inputs the
chain does not build). Freshness is judged with the selector the builders use (``select_best`` -- see
``derived_daily.inputs_stale``), never with ``ArtifactCatalog.locate``. A partition with no recorded input version
cannot be judged: it is UNDECIDABLE, not fresh. Building is done by the registry ``fetch_job`` of each dataset; the
sweep only looks at exit codes (0 built, 20 upstream not ready, 30 retryable, 40 deterministic).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .derived_daily import inputs_stale, select_best

FRESH, STALE, MISSING, UNDECIDABLE = "FRESH", "STALE", "MISSING", "UNDECIDABLE"
NEEDS_BUILD = {STALE, MISSING, UNDECIDABLE}
CHAINS_PATH = Path(__file__).resolve().parents[1] / "guardian" / "chains.json"

Snapshot = Mapping[str, Any]
ManifestReader = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]
StatusFn = Callable[[Snapshot, str, str], tuple[str, str]]
Runner = Callable[[str, str], int]


class ChainConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ChainConfig:
    name: str
    base: str
    window_days: int
    max_builds_per_run: int
    nodes: Mapping[str, tuple[str, ...]]
    external: frozenset[str]

    def order(self) -> list[str]:
        """Nodes in dependency order (requirements first)."""
        done: list[str] = []
        pending = dict(self.nodes)
        while pending:
            ready = sorted(name for name, needs in pending.items()
                           if all(need in done or need in self.external or need not in self.nodes for need in needs))
            if not ready:                    # only cyclic nodes are left; the caller compares lengths and rejects it
                break
            done.extend(ready)
            for name in ready:
                pending.pop(name)
        return done


def load_chain_config(name: str, source: Mapping[str, Any] | str | Path | None = None,
                      registry_ids: Iterable[str] | None = None) -> ChainConfig:
    raw = source if isinstance(source, Mapping) else json.loads(Path(source or CHAINS_PATH).read_text(encoding="utf-8"))
    chain = (raw.get("chains") or {}).get(name)
    if not isinstance(chain, Mapping):
        raise ChainConfigError("unknown_chain:" + name)
    nodes = {str(key): tuple(str(item) for item in value) for key, value in (chain.get("nodes") or {}).items()}
    external = frozenset(str(item) for item in chain.get("external") or ())
    window, cap = chain.get("window_days"), chain.get("max_builds_per_run")
    if type(window) is not int or window < 1 or type(cap) is not int or cap < 1:
        raise ChainConfigError("window_days_and_max_builds_per_run_must_be_positive_integers")
    if not nodes:
        raise ChainConfigError("chain_has_no_nodes")
    for node, needs in nodes.items():
        for need in needs:
            if need not in nodes and need not in external:
                raise ChainConfigError(f"unknown_requirement:{node}->{need}")
    base = str(chain.get("base") or "")
    if base not in external:
        raise ChainConfigError("base_must_be_an_external_dataset")
    config = ChainConfig(name, base, window, cap, nodes, external)
    if len(config.order()) != len(nodes):                  # a cycle leaves nodes that never become ready
        raise ChainConfigError("dependency_cycle")
    if registry_ids is not None:
        missing = sorted(set(nodes) - set(registry_ids))
        if missing:
            raise ChainConfigError("node_without_registry_fetch_job:" + ",".join(missing))
    return config


def entries(snapshot: Snapshot, dataset: str, day: str) -> list[Mapping[str, Any]]:
    found = snapshot.get("datasets", {}).get(dataset, {}).get("dates", {}).get(day) or []
    return [item for item in found if isinstance(item, Mapping)]


def exists(snapshot: Snapshot, dataset: str, day: str) -> bool:
    return bool(entries(snapshot, dataset, day))


def read_manifest_file(entry: Mapping[str, Any]) -> Mapping[str, Any] | None:
    try:
        value = json.loads(Path(str(entry.get("manifest") or "")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def make_status_fn(read_manifest: ManifestReader = read_manifest_file) -> StatusFn:
    def node_status(snapshot: Snapshot, dataset: str, day: str) -> tuple[str, str]:
        found = entries(snapshot, dataset, day)
        if not found:
            return MISSING, "no_partition"
        best = select_best(found)
        manifest = read_manifest(best) if best else None
        if manifest is None:
            return UNDECIDABLE, "manifest_unreadable"
        recorded = bool(manifest.get("input_refs")) or bool((manifest.get("inputs") or {}).get("daily_kline"))
        if not recorded:
            return UNDECIDABLE, "no_recorded_inputs"
        if inputs_stale(snapshot, dataset, day, manifest):
            return STALE, "input_version_changed"
        return FRESH, ""
    return node_status


def chain_days(snapshot: Snapshot, config: ChainConfig, today: str) -> list[str]:
    """The newest ``window_days`` days that have the chain's base input."""
    days = sorted(day for day in snapshot.get("datasets", {}).get(config.base, {}).get("dates", {}) if day <= today)
    return days[-config.window_days:]


@dataclass(frozen=True)
class PlanItem:
    dataset: str
    day: str
    status: str
    reason: str
    key: tuple

    def as_dict(self) -> dict[str, str]:
        return {"dataset": self.dataset, "day": self.day, "status": self.status, "reason": self.reason}


def _input_signature(snapshot: Snapshot, needs: Sequence[str], day: str) -> tuple:
    out = []
    for need in needs:
        best = select_best(entries(snapshot, need, day))
        out.append((need, best.get("version") if best else None))
    return tuple(out)


def plan(snapshot: Snapshot, config: ChainConfig, days: Sequence[str], status_fn: StatusFn
         ) -> tuple[list[PlanItem], list[dict[str, str]]]:
    """(items to build, blocked). Newest day first, requirements before dependents inside a day."""
    items: list[PlanItem] = []
    blocked: list[dict[str, str]] = []
    order = config.order()
    for day in sorted(days, reverse=True):
        planned_today: set[str] = set()
        for dataset in order:
            status, reason = status_fn(snapshot, dataset, day)
            if status not in NEEDS_BUILD:
                continue
            needs = config.nodes[dataset]
            unmet = [need for need in needs
                     if not exists(snapshot, need, day) and need not in planned_today]
            if unmet:
                blocked.append({"dataset": dataset, "day": day, "status": status, "missing": ",".join(unmet)})
                continue
            planned_today.add(dataset)
            items.append(PlanItem(dataset, day, status, reason, (dataset, day, _input_signature(snapshot, needs, day))))
    return items, blocked


def sweep(snapshot_fn: Callable[[], Snapshot], config: ChainConfig, *, today: str, runner: Runner,
          status_fn: StatusFn, lease: Any = None, dry_run: bool = False, budget_seconds: float | None = None,
          clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Build what is missing or stale, one node at a time, re-reading the catalog after every build."""
    started = clock()
    if lease is not None:
        try:
            acquired = lease.acquire()
        except Exception as exc:
            return {"status": "RETRY", "reason": "lease_unavailable:" + type(exc).__name__}
        if not acquired:
            return {"status": "BUSY"}
    try:
        days = chain_days(snapshot_fn(), config, today)
        items, blocked = plan(snapshot_fn(), config, days, status_fn)
        if dry_run:
            return {"status": "PLAN", "days": days, "plan": [item.as_dict() for item in items], "blocked": blocked}
        built: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        retry: list[dict[str, Any]] = []
        done: set = set()
        not_built: set[tuple[str, str]] = set()      # (dataset, day) whose build returned non-zero this run
        capped = False
        while True:
            items, blocked = plan(snapshot_fn(), config, days, status_fn)
            # a dependent is not built on top of an upstream that just failed: it would be stale again at once
            todo = [item for item in items if item.key not in done
                    and not any((need, item.day) in not_built for need in config.nodes[item.dataset])]
            if not todo:
                break
            if len(done) >= config.max_builds_per_run or (
                    budget_seconds is not None and clock() - started >= budget_seconds):
                capped = True              # the next sweep continues; the event is never killed mid-build by its timeout
                break
            item = todo[0]
            done.add(item.key)
            if lease is not None and not lease.renew():
                retry.append({**item.as_dict(), "code": 30, "reason": "lease_lost"})
                break
            code = runner(item.dataset, item.day)
            record = {**item.as_dict(), "code": code}
            if code == 0:
                built.append(record)
            elif code in (20, 30):
                retry.append(record)           # upstream not ready / transient: a later sweep tries again
                not_built.add((item.dataset, item.day))
            else:
                failed.append(record)
                not_built.add((item.dataset, item.day))
        items, blocked = plan(snapshot_fn(), config, days, status_fn)
        not_converged = [item.as_dict() for item in items
                         if item.key in done and any(b["dataset"] == item.dataset and b["day"] == item.day
                                                     for b in built)]
        remaining = [item.as_dict() for item in items]
        waiting = [item.as_dict() for item in items if item.key not in done
                   and any((need, item.day) in not_built for need in config.nodes[item.dataset])]
        return {"status": "DONE", "days": days, "built": built, "retry": retry, "failed": failed,
                "not_converged": not_converged, "remaining": remaining, "waiting_on_failed_upstream": waiting,
                "blocked": blocked, "capped": capped,
                "elapsed_seconds": round(clock() - started, 3)}
    finally:
        if lease is not None:
            lease.release()


def exit_code(result: Mapping[str, Any]) -> int:
    """0 built everything that was due, 20 nothing to do, 30 retry later, 40 a node fails deterministically."""
    status = result.get("status")
    if status == "BUSY":
        return 20
    if status in {"RETRY"}:
        return 30
    if status == "PLAN":
        return 0
    if result.get("failed") or result.get("not_converged"):
        return 40
    if result.get("retry") or result.get("capped") or result.get("remaining"):
        return 30
    return 0 if result.get("built") else 20
