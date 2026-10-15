"""Deterministic, stage-scoped inputs for the short-review story layer."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

STAGES = ("auction", "open", "intraday", "close")
_LATER = ("close", "afternoon", "next_day", "winner", "result")


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(k): _json_value(v) for k, v in value.items() if not callable(v)}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(v) for v in value]
    return str(value)


def _stage_value(key: str, value: Any, stage: str) -> bool:
    name = key.lower()
    if stage != "close" and any(token in name for token in _LATER):
        return False
    if stage == "auction":
        return not any(token in name for token in ("p0932", "p0945", "open_", "intraday"))
    if stage == "open":
        return not any(token in name for token in ("p0945", "intraday"))
    if stage == "intraday":
        return True
    return True


def _at_or_before(value: Any, cutoff: str) -> bool:
    """Compare explicit HH:MM[:SS] cutoffs; unparseable lineage fails closed."""
    if not value or not cutoff:
        return True
    def seconds(raw: Any) -> int | None:
        text = str(raw).split("T")[-1].split("+")[0].split("-")[0]
        parts = text.split(":")
        if len(parts) < 2:
            return None
        try:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + (int(parts[2]) if len(parts) > 2 else 0)
        except ValueError:
            return None
    left, right = seconds(value), seconds(cutoff)
    return left is not None and right is not None and left <= right


def build_story_features(facts: Mapping[str, Any], *, stage: str, cutoff: str,
                         observed_at: str | None, provenance: Mapping[str, Any],
                         prior_memory: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Project only facts knowable at ``stage``; missing values remain explicit."""
    stage = str(stage).lower()
    if stage not in STAGES:
        raise ValueError(f"unsupported story stage: {stage}")
    trade_date = str(facts.get("trade_date") or facts.get("date") or "")
    metrics: dict[str, Any] = {}
    raw_metrics = facts.get("metrics") if isinstance(facts.get("metrics"), Mapping) else {}
    candidates = {**facts, **raw_metrics}
    for key, raw in candidates.items():
        if key in {"trade_date", "date", "metrics", "story_stocks", "stocks", "funnel_display",
                   "funnel_counts", "candidate_refs", "quality", "source_refs"}:
            continue
        if not _stage_value(str(key), raw, stage):
            continue
        if isinstance(raw, Mapping) and "value" in raw:
            value = raw.get("value")
            source = dict(raw)
        elif isinstance(raw, (str, int, float, bool)) or raw is None:
            value = raw
            source = {}
        else:
            continue
        field_cutoff = source.get("event_cutoff", source.get("cutoff"))
        if not _at_or_before(field_cutoff, cutoff):
            continue
        if value is None and not source.get("missing_reason"):
            source["missing_reason"] = "not supplied"
        metrics[str(key)] = {
            "metric_id": str(source.get("metric_id") or key), "stage": stage,
            "event_cutoff": str(source.get("event_cutoff") or cutoff),
            "available_at": source.get("available_at"), "observed_at": source.get("observed_at", observed_at),
            "value": _json_value(value), "unit": source.get("unit"),
            "numerator": _json_value(source.get("numerator")),
            "denominator": _json_value(source.get("denominator")),
            "universe_id": str(source.get("universe_id") or "UNKNOWN"),
            "source_fidelity": str(source.get("source_fidelity") or provenance.get("source_fidelity", "UNKNOWN")),
            "price_status": str(source.get("price_status") or "UNKNOWN"),
            "membership_asof": source.get("membership_asof"),
            "coverage": _json_value(source.get("coverage")), "lag_seconds": _json_value(source.get("lag_seconds")),
            "missing_reason": source.get("missing_reason") if value is None else None,
            "refs": _json_value(source.get("refs", provenance.get("refs", []))),
            "quality": _json_value(source.get("quality", [])),
        }
    return {"schema": "FeatureBundleV1", "trade_date": trade_date, "stage": stage,
            "cutoff": str(cutoff), "observed_at": observed_at, "metrics": metrics,
            "candidate_refs": _json_value(facts.get("candidate_refs", [])),
            "source_refs": _json_value(facts.get("source_refs", [])),
            "quality": _json_value(facts.get("quality", [])),
            "prior_memory_refs": [str(x.get("trade_date", "")) for x in (prior_memory or ())]}
