"""Optional append-only daily story memory; never writes unless explicitly asked."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any


def make_daily_memory(story: Mapping[str, Any], features: Mapping[str, Any], *,
                      input_refs: Sequence[Mapping[str, Any]], rule_version: str) -> dict[str, Any]:
    metrics = []
    for metric in features.get("metrics", {}).values():
        record = {k: metric.get(k) for k in ("metric_id", "stage", "value", "unit", "available_at",
            "observed_at", "universe_id", "quality", "missing_reason", "refs", "numerator", "denominator",
            "membership_asof", "price_status", "coverage", "lag_seconds", "source_fidelity")}
        record["cutoff"] = metric.get("event_cutoff", metric.get("cutoff"))
        metrics.append(record)
    fidelity_set = {m.get("source_fidelity") for m in metrics if m.get("source_fidelity")}
    fidelity = "OBSERVED_AT_TIME" if fidelity_set == {"OBSERVED_AT_TIME"} else "HISTORICAL_AVAILABILITY_UNKNOWN" if not fidelity_set or "UNKNOWN" in fidelity_set else "MIXED"
    funnel_rows = [row for bucket in (story.get("funnel") or {}).values() for row in bucket]
    return {"schema": "DailyStoryMemoryV1", "trade_date": story.get("trade_date"), "rule_version": rule_version,
            "generated_at": story.get("generated_at"), "fidelity": fidelity,
            "inputs": [dict(x) for x in input_refs][:128], "metrics": metrics[:256],
            "story_tags": [str(x.get("id")) for x in story.get("top_events", [])][:7],
            "acts": list(story.get("acts", []))[:4],
            "display_candidates": [{"symbol": x.get("symbol", ""), "name": x.get("name", ""),
                "asof": x.get("asof", ""), "bucket": x.get("bucket"), "reasons": x.get("reasons", []),
                "condition": x.get("condition"), "risk": x.get("risk", ""), "refs": x.get("origin_refs", [])}
                for x in funnel_rows[:15]], "next_day_ref": None}


def attach_next_day(memory_ref: str, outcomes: Sequence[Mapping[str, Any]], *, observed_at: str) -> dict[str, Any]:
    return {"schema": "NextDayOutcomeV1", "memory_ref": str(memory_ref), "observed_at": observed_at,
            "outcomes": [dict(x) for x in outcomes]}


def append_memory(record: Mapping[str, Any], *, directory: str | Path, retention_days: int = 128) -> str:
    """Append one JSON line and prune dates older than retention; opt-in via explicit directory."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{record.get('trade_date', 'unknown')}.json"
    payload = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    path.write_text(payload, encoding="utf-8")
    dated = sorted(root.glob("????-??-??.json"), key=lambda p: p.stem)
    for item in dated[:-max(1, retention_days)]:
        item.unlink()
    return str(path)
