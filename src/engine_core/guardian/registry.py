"""Read and validate the data inventory used by guardian health checks."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REGISTRY_PATH = Path(__file__).with_name("datasets.registry.json")
PHASES = frozenset({
    "NIGHT", "PRE_MARKET", "AUCTION", "AUCTION_END", "OPEN",
    "INTRADAY_AM", "MIDDAY_BREAK", "PM_OPEN", "INTRADAY_PM",
    "CLOSE_AUCTION", "CLOSE", "POST_MARKET",
})
_REQUIRED = {"id", "owner", "artifact", "phases", "cadence", "rule",
             "freshness", "retry", "degrade", "lifecycle", "notes", "guardian_event"}
# A daily-bar partition is only "present" when it covers (almost) the whole universe: a checkpointed partial capture
# (e.g. 1774 of 5224 symbols after a timed-out attempt) must keep being fetched, and nothing may derive from it.
MIN_KLINE_COVERAGE = 0.99


def kline_coverage_problem(quality: Any) -> str | None:
    """None when a daily_kline manifest quality block is complete enough; else a short reason."""
    if not isinstance(quality, Mapping):
        return None
    coverage = quality.get("coverage")
    if isinstance(coverage, (int, float)) and coverage < MIN_KLINE_COVERAGE:
        return f"daily_kline_partial:{coverage:.3f}"
    return None


def catalog_manifest(catalog: Mapping[str, Any], dataset: str, day: str | None) -> dict[str, Any] | None:
    """Read the newest catalogued manifest for one partition, if it is retained."""
    record = catalog.get("datasets", {}).get(dataset, {})
    versions = record.get("dates", {}).get(day, []) if day else []
    if isinstance(versions, Mapping):
        versions = [versions]
    for version in reversed(versions if isinstance(versions, list) else []):
        if not isinstance(version, Mapping):
            continue
        metadata = dict(version)
        manifest_path = version.get("manifest")
        if manifest_path:
            try:
                manifest = json.loads(Path(str(manifest_path)).read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if isinstance(manifest, Mapping):
                metadata = {**manifest, **metadata}
        if metadata.get("version"):
            return metadata
    return None


def daily_kline_needs_upgrade(manifest: Mapping[str, Any] | None) -> bool:
    if not isinstance(manifest, Mapping):
        return False
    quality = manifest.get("quality")
    status = quality.get("status") if isinstance(quality, Mapping) else quality
    completeness = manifest.get("field_completeness")
    if not isinstance(completeness, Mapping) and isinstance(quality, Mapping):
        completeness = quality.get("field_completeness")
    return (str(status or "").upper() in {"DEGRADED", "PROVISIONAL", "PARTIAL"}
            or str(manifest.get("business_status", "")).upper() == "PROVISIONAL"
            or bool(manifest.get("degraded"))
            or (isinstance(completeness, Mapping)
                and str(completeness.get("open", "")).upper() in {"MISSING", "PARTIAL"}))


DERIVED_DAILY = frozenset({"daily_indicators", "daily_report_features"})


def _best_entry_manifest(catalog: Mapping[str, Any], dataset: str, day: str) -> dict[str, Any] | None:
    """Manifest of the highest-quality catalogued version of one partition (newest wins ties)."""
    from ..data_pipeline.derived_daily import select_best
    entries = catalog.get("datasets", {}).get(dataset, {}).get("dates", {}).get(day) or []
    if isinstance(entries, Mapping):
        entries = [entries]
    best = select_best([e for e in entries if isinstance(e, Mapping)])
    if best is None:
        return None
    metadata = dict(best)
    try:
        manifest = json.loads(Path(str(best["manifest"])).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, KeyError):
        return metadata
    return {**manifest, **metadata} if isinstance(manifest, Mapping) else metadata


def derived_needs_build(catalog: Mapping[str, Any], dataset: str, day: str, now: datetime,
                        min_status: str = "PROVISIONAL") -> str | None:
    """Why a derived daily partition must be (re)built now, or None.

    Event gate, not a clock: the partition's own daily_kline must be present (coverage, availability) at
    ``min_status`` or better. Then it is due when the derived partition is missing, or one of its recorded input
    versions is no longer the best catalogued version (e.g. daily_kline PROVISIONAL -> FORMAL).
    """
    from ..data_pipeline.derived_daily import STATUS_RANK, inputs_stale, normalize_status
    kline = _best_entry_manifest(catalog, "daily_kline", day)
    if kline is None or not catalog_partition_present(catalog, "daily_kline", day, now):
        return None
    if STATUS_RANK[normalize_status(kline.get("business_status"))] < STATUS_RANK[normalize_status(min_status)]:
        return None
    if not catalog_partition_present(catalog, "symbol_universe", day, now):
        return None   # coverage denominator not ready; never derive against an unknown universe
    derived = _best_entry_manifest(catalog, dataset, day)
    if derived is None or not catalog_partition_present(catalog, dataset, day, now):
        return "missing"
    if inputs_stale(catalog, dataset, day, derived):
        return "inputs_changed"
    return None


def derived_daily_kline_input_stale(catalog: Mapping[str, Any], dataset: str, day: str) -> bool:
    """Old manifests without inputs are intentionally unknown, not retroactively stale."""
    if dataset not in {"daily_indicators", "ladder_daily", "stock_day_facts", "daily_report_features"}:
        return False
    derived = catalog_manifest(catalog, dataset, day)
    if (dataset in DERIVED_DAILY and isinstance(derived, Mapping) and derived.get("input_refs")):
        from ..data_pipeline.derived_daily import inputs_stale
        return inputs_stale(catalog, dataset, day, derived)
    inputs = derived.get("inputs") if isinstance(derived, Mapping) else None
    recorded = inputs.get("daily_kline") if isinstance(inputs, Mapping) else None
    if not isinstance(recorded, Mapping) or not recorded.get("version"):
        return False
    current = catalog_manifest(catalog, "daily_kline", day)
    return bool(current and current.get("version") and recorded["version"] != current["version"])


_NONEMPTY_DATASETS = frozenset({"daily_kline", "daily_indicators", "symbol_universe",
                               "stock_day_facts", "auction_stock_facts", "plate_members"})


def catalog_partition_present(catalog: Mapping[str, Any], dataset: str, day: str | None,
                              now: datetime) -> bool:
    record = catalog.get("datasets", {}).get(dataset)
    versions = record.get("dates", {}).get(day) if day and isinstance(record, Mapping) else None
    if not versions:
        return False
    if dataset not in _NONEMPTY_DATASETS:
        return bool(versions)
    if not isinstance(versions, list):
        versions = [versions]
    for version in versions:
        if not isinstance(version, Mapping):
            continue
        metadata: Mapping[str, Any] = version
        manifest = version.get("manifest")
        if manifest:
            try:
                loaded = json.loads(Path(str(manifest)).read_text(encoding="utf-8"))
                if isinstance(loaded, Mapping):
                    metadata = {**loaded, **version}
            except (OSError, ValueError, TypeError):
                continue
        try:
            if int(metadata.get("row_count", 0) or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        quality = metadata.get("quality")
        quality_status = quality.get("status") if isinstance(quality, Mapping) else quality
        if str(quality_status or "").upper() == "FAILED":
            continue
        if dataset == "daily_kline" and kline_coverage_problem(quality):
            continue
        available_at = metadata.get("available_at")
        if not available_at:
            continue
        try:
            available = datetime.fromisoformat(str(available_at).replace("Z", "+00:00"))
            if available.tzinfo is None:
                available = available.replace(tzinfo=now.tzinfo)
            current = now if now.tzinfo else now.astimezone()
            if available <= current:
                return True
        except (TypeError, ValueError, OverflowError):
            continue
    return False


def load_registry(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load the checked-in registry and reject malformed or duplicate entries."""
    source = Path(path) if path is not None else REGISTRY_PATH
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload.get("phase_names", [])) != PHASES or len(payload.get("phase_names", [])) != 12:
        raise ValueError("registry phase_names must declare the 12 supported phases")
    items = payload.get("items")
    validate_registry(items)
    from .scheduler import _schedule_source
    events = {event["event_id"]: event for event in _schedule_source().get("events", [])}
    for item in items:
        policy = item.get("late_policy")
        if not isinstance(policy, dict) or not policy.get("trigger_event"):
            continue
        event = events.get(policy["trigger_event"])
        start = event.get("start") if event else None
        if event and start is None:
            start = event.get("at")
            if isinstance(start, list):
                start = start[0] if start else None
        if not event or event.get("trigger_type") != "window" or not start:
            raise ValueError(f"registry trigger_event invalid: {item['id']}:{policy['trigger_event']}")
        # Keep the physical registry as an event reference; consumers receive a
        # resolved compatibility value so existing reconcile logic stays stable.
        policy["trigger_at"] = start
    return items


def validate_registry(items: Any) -> int:
    """Validate entries and return the count of explicit UNKNOWN values."""
    if not isinstance(items, list):
        raise ValueError("registry items must be a list")
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"item {index} must be an object")
        missing = _REQUIRED - item.keys()
        if missing:
            raise ValueError(f"item {index} missing required fields: {sorted(missing)}")
        if not isinstance(item["id"], str) or not item["id"] or item["id"] in seen:
            raise ValueError(f"item {index} has empty or duplicate id")
        seen.add(item["id"])
        if not isinstance(item["phases"], list) or not item["phases"]:
            raise ValueError(f"item {item['id']} phases must be a non-empty list")
        invalid = set(item["phases"]) - PHASES
        if invalid:
            raise ValueError(f"item {item['id']} has invalid phase(s): {sorted(invalid)}")
        if not isinstance(item["artifact"], dict) or not isinstance(item["lifecycle"], dict):
            raise ValueError(f"item {item['id']} artifact and lifecycle must be objects")
        if not isinstance(item["degrade"], list):
            raise ValueError(f"item {item['id']} degrade must be a list")
        for field in ("owner", "cadence", "rule", "freshness", "retry", "notes"):
            if item[field] is None and field != "owner":
                raise ValueError(f"item {item['id']} field {field} must be explicit")
    return _count_unknown(items)


def _count_unknown(value: Any) -> int:
    if value == "UNKNOWN":
        return 1
    if isinstance(value, Mapping):
        return sum(_count_unknown(child) for child in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_count_unknown(child) for child in value)
    return 0


def items_for_phase(phase: str, registry: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Return entries expected or allowed in one of the 12 design phases."""
    if phase not in PHASES:
        raise ValueError(f"invalid phase: {phase}")
    items = load_registry() if registry is None else registry
    validate_registry(items)
    return [item for item in items if phase in item["phases"]]


def stale_items(now: datetime, catalog: str | Path | Mapping[str, Any],
                registry: list[dict[str, Any]] | None = None, *,
                trade_date: str | None = None, prev_trade_date: str | None = None) -> list[dict[str, Any]]:
    """Per-item status for a trading day, from registry metadata and the catalog only.

    ``freshness`` = {partition: "D"|"D-1", expected_by: "HH:MM"}. The item is OK when the
    catalog holds that partition, PENDING before ``expected_by`` (local time of ``now``),
    OVERDUE after it. Items without a structured rule or catalog dataset stay UNKNOWN;
    a dataset absent from the catalog entirely is MISSING.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    payload = catalog if isinstance(catalog, Mapping) else json.loads(Path(catalog).read_text(encoding="utf-8"))
    datasets = payload.get("datasets", {}) if isinstance(payload, Mapping) else {}
    items = load_registry() if registry is None else registry
    validate_registry(items)
    day = trade_date or now.date().isoformat()
    result = []
    for item in items:
        dataset = item["artifact"].get("catalog_dataset")
        rule = item["freshness"]
        if item.get("retired"):
            result.append({"id": item["id"], "status": "RETIRED", "dataset": dataset,
                           "reason": str(item["retired"].get("reason", ""))[:120]})
            continue
        if not dataset:
            result.append({"id": item["id"], "status": "UNKNOWN", "reason": "no catalog dataset"})
            continue
        record = datasets.get(dataset)
        if not isinstance(record, Mapping):
            result.append({"id": item["id"], "status": "MISSING", "dataset": dataset})
            continue
        if not isinstance(rule, Mapping) or "partition" not in rule or ("expected_by" not in rule and rule["partition"] != "latest"):
            result.append({"id": item["id"], "status": "UNKNOWN", "dataset": dataset,
                           "reason": "no structured freshness rule"})
            continue
        if rule["partition"] == "latest":
            keys = sorted(k for k in record.get("dates", {}) if catalog_partition_present(payload, dataset, k, now))
            age = (datetime.fromisoformat(day) - datetime.fromisoformat(keys[-1])).days if keys else None
            result.append({"id": item["id"], "dataset": dataset, "partition": keys[-1] if keys else None,
                           "status": "MISSING" if age is None else ("OK" if age <= rule.get("max_age_days", 8) else "STALE")})
            continue
        want = day if rule["partition"] == "D" else prev_trade_date
        if not want:
            result.append({"id": item["id"], "status": "UNKNOWN", "dataset": dataset,
                           "reason": "partition date unavailable"})
            continue
        present = catalog_partition_present(payload, dataset, want, now)
        lineage_stale = present and derived_daily_kline_input_stale(payload, dataset, want)
        hh, mm = (int(part) for part in str(rule["expected_by"]).split(":"))
        due = (now.hour, now.minute) >= (hh, mm)
        status = "OVERDUE" if lineage_stale else ("OK" if present else ("OVERDUE" if due else "PENDING"))
        result.append({"id": item["id"], "dataset": dataset, "status": status,
                       "partition": want, "expected_by": rule["expected_by"],
                       **({"reason": "daily_kline_version_changed"} if lineage_stale else {})})
    return result
