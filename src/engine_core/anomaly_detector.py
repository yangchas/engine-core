"""Small-sample-aware descriptive comparisons; never creates a composite score."""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any


def _family(metric_id: str) -> str:
    name = metric_id.lower()
    for token, family in (("breadth", "breadth"), ("amount", "volume"), ("volume", "volume"),
                          ("limit", "limit"), ("ladder", "ladder"), ("plate", "structure"),
                          ("theme", "structure"), ("median", "breadth")):
        if token in name:
            return family
    return "context"


def _number(row: Mapping[str, Any]) -> float | None:
    value = row.get("value")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def detect_anomalies(current: Sequence[Mapping[str, Any]], history: Sequence[Mapping[str, Any]], *,
                     lookback: int = 64, policy: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    policy = policy or {}
    hist_by_metric: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in history:
        hist_by_metric[str(row.get("metric_id", row.get("metric", "")))].append(row)
    out = []
    for row in current:
        metric = str(row.get("metric_id", row.get("metric", "")))
        stage = str(row.get("stage", "unknown"))
        value = _number(row)
        hist = [r for r in hist_by_metric.get(metric, ()) if str(r.get("stage", stage)) == stage and _number(r) is not None][-max(0, int(lookback)):]
        values = [_number(r) for r in hist]
        values = [v for v in values if v is not None]
        n = len(values)
        kind, quality, p, z, rank = "UNKNOWN", ["MISSING_NOT_ZERO"], None, None, None
        if value is None:
            kind = "UNKNOWN"
        elif n < 3:
            kind, quality = "UNKNOWN", ["HISTORY_UNDER_3"]
        else:
            below = sum(x < value for x in values)
            equal = sum(x == value for x in values)
            p = (below + 0.5 * equal) / n
            rank = 1 + sum(x > value for x in values)
            median = statistics.median(values)
            mad = statistics.median(abs(x - median) for x in values)
            if mad:
                z = (value - median) / (1.4826 * mad)
            denominator = row.get("denominator")
            if n < int(policy.get("rank_only_min_days", 5)):
                kind, quality = "CONTEXT", ["SHORT_HISTORY_CONTEXT_ONLY"]
            elif isinstance(denominator, (int, float)) and denominator < int(policy.get("small_denominator_below", 5)):
                kind, quality = "OBSERVED_RANK", ["SMALL_DENOMINATOR_RANK_ONLY"]
            elif n >= int(policy.get("tail_min_days", 20)):
                if p >= float(policy.get("tail_percentile", (0.05, 0.95))[1]) or p <= float(policy.get("tail_percentile", (0.05, 0.95))[0]):
                    kind, quality = "TAIL", ["DESCRIPTIVE_THRESHOLD"]
                elif z is not None and abs(z) >= float(policy.get("abs_robust_z", 3)):
                    kind, quality = "ROBUST", ["DESCRIPTIVE_THRESHOLD"]
                else:
                    kind, quality = "CONTEXT", ["NO_HISTORICAL_EXTREME"]
            else:
                kind, quality = "OBSERVED_RANK", ["SHORT_HISTORY_RANK_ONLY"]
        out.append({"id": f"{stage}:{metric}", "metric_ids": [metric], "family": _family(metric),
                    "stage": stage, "kind": kind, "quality": quality, "n": n,
                    "history_start": hist[0].get("trade_date") if hist else None,
                    "history_end": hist[-1].get("trade_date") if hist else None,
                    "value": value, "p": p, "robust_z": z, "ranks": {"descending": rank, "n": n},
                    "refs": list(row.get("refs", [])), "display_eligible": kind in {"TAIL", "ROBUST", "OBSERVED_RANK"}})
    # Keep the strongest single comparable event per family; ties resolve by metric id.
    chosen = {}
    priority = {"TAIL": 3, "ROBUST": 2, "OBSERVED_RANK": 1, "CONTEXT": 0, "UNKNOWN": -1}
    unknown_rows = [row for row in out if row["kind"] == "UNKNOWN"]
    for row in out:
        if row["kind"] == "UNKNOWN":
            continue
        family = row["family"]
        if family not in chosen or (priority[row["kind"]], row["id"]) > (priority[chosen[family]["kind"]], chosen[family]["id"]):
            chosen[family] = row
    return sorted([*chosen.values(), *unknown_rows], key=lambda x: (-priority[x["kind"]], x["family"], x["id"]))
