"""Deterministic, stage-scoped inputs for the short-review story layer."""
from __future__ import annotations

import math
import re
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
        return not any(token in name for token in ("intraday",))
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


def _safe_rows(rows: Any, *, stage: str, cutoff: str) -> list[Any]:
    """Remove later checkpoint and post-close fields from nested live rows."""
    def clean(value: Any) -> Any:
        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                name = str(key).lower()
                if any(token in name for token in ("close", "afternoon", "next_day", "winner", "result")):
                    continue
                match = re.search(r"p(\d{4})", name)
                if match:
                    hhmm = match.group(1)
                    if stage == "auction" or int(hhmm[:2]) * 60 + int(hhmm[2:]) > _cutoff_minutes(cutoff):
                        continue
                result[str(key)] = clean(item)
            return result
        if isinstance(value, list):
            return [clean(item) for item in value]
        return _json_value(value)
    return clean(rows) if isinstance(rows, list) else []


def _cutoff_minutes(value: str) -> int:
    try:
        parts = str(value).split("T")[-1].split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except (TypeError, ValueError, IndexError):
        return -1


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


def build_live_story_features(report: Mapping[str, Any], *, stage: str, cutoff: str,
                              observed_at: str | None = None) -> dict[str, Any]:
    """Adapt live report sections into the same cutoff-checked story feature bundle.

    This deliberately reads only the active auction/open slice and the auction handoff
    for an open report. The full report remains available to the renderer as appendices.
    """
    chapters = report.get("chapters") if isinstance(report.get("chapters"), Mapping) else {}
    market = report.get("market_price_volume")
    if not isinstance(market, Mapping):
        market = chapters.get("market_price_volume", {})
    market = market if isinstance(market, Mapping) else {}
    facts: dict[str, Any] = {"trade_date": report.get("trade_date") or report.get("date")}
    refs = ["live_report:market_price_volume"]
    def add(key: str, value: Any, source: str, unit: str | None = None,
            event_cutoff: Any = None) -> None:
        facts[key] = {"value": value, "unit": unit, "refs": [source],
                      "event_cutoff": event_cutoff or market.get("asof") or cutoff,
                      "missing_reason": "not present in live report" if value is None else None}

    phase = str(market.get("phase") or "").upper()
    target = "auction" if stage == "auction" else "open"
    # In the open report the 09:25 terminal observation comes from the same-day handoff.
    handoff = report.get("handoff") if isinstance(report.get("handoff"), Mapping) else {}
    auction_handoff = handoff.get("auction_0925") if isinstance(handoff.get("auction_0925"), Mapping) else {}
    terminal = auction_handoff.get("final_0925") if isinstance(auction_handoff.get("final_0925"), Mapping) else {}
    if stage == "auction":
        terminal = report.get("auction_terminal_0925") if isinstance(report.get("auction_terminal_0925"), Mapping) else terminal
    if terminal:
        event_at = terminal.get("asof") or "09:25:06"
        valid_terminal = terminal.get("status") not in {"STALE", "NOT_READY", "NOT_YET_AVAILABLE"} and terminal.get("symbol_count") != 0
        add("auction_amount_yuan", terminal.get("market_match_amount_yuan") if valid_terminal else None,
            "auction_terminal_0925", "yuan", event_at)
        add("auction_median_pct", terminal.get("median_pct_change") if valid_terminal else None,
            "auction_terminal_0925", "pp", event_at)
        share = terminal.get("up_share_pct") if valid_terminal else None
        add("auction_up_share", float(share) / 100 if isinstance(share, (int, float)) else None,
            "auction_terminal_0925", "ratio", event_at)
        add("auction_prev_common_ratio", terminal.get("vs_previous_day_final") if valid_terminal else None,
            "auction_terminal_0925", "ratio", event_at)
        baseline = terminal.get("auction_baseline_comparison")
        percentile = baseline.get("percentile") if valid_terminal and isinstance(baseline, Mapping) else None
        add("auction_baseline_percentile", percentile,
            "auction_terminal_0925", "percentile", event_at)
        add("auction_baseline_multiple", baseline.get("multiple") if valid_terminal and isinstance(baseline, Mapping) else None,
            "auction_terminal_0925", "ratio", event_at)
        add("auction_baseline_median_yuan", float(baseline.get("median_yi")) * 1e8
            if valid_terminal and isinstance(baseline, Mapping) and isinstance(baseline.get("median_yi"), (int, float)) else None,
            "auction_terminal_0925", "yuan", event_at)
        add("auction_limit_up_price_count", terminal.get("limit_up") if valid_terminal else None, "auction_terminal_0925", "count", event_at)
        add("auction_limit_down_price_count", terminal.get("limit_down") if valid_terminal else None, "auction_terminal_0925", "count", event_at)
    if market and (stage == "auction" or phase in {"AUCTION", "OPEN"}):
        prefix = "auction" if stage == "auction" else (
            "p0945" if _at_or_before("09:45:00", cutoff) else "open")
        event_at = market.get("asof") or cutoff
        valid_market = (market.get("status") not in {"STALE", "NOT_READY", "NOT_YET_AVAILABLE"}
                        and market.get("symbol_count") != 0 and _at_or_before(event_at, cutoff))
        amount = market.get("market_match_amount_yuan") if valid_market else None
        median = market.get("median_pct_change") if valid_market else None
        share = market.get("up_share_pct") if valid_market else None
        add(f"{prefix}_amount_yuan", amount, "market_price_volume", "yuan", event_at)
        add(f"{prefix}_median_pct", median, "market_price_volume", "pp", event_at)
        add(f"{prefix}_up_share", float(share) / 100 if isinstance(share, (int, float)) else None,
            "market_price_volume", "ratio", event_at)
        if stage == "open":
            add("open_median_pct", median, "market_price_volume", "pp", event_at)
            add("open_up_share", float(share) / 100 if isinstance(share, (int, float)) else None,
                "market_price_volume", "ratio", event_at)
            add("open_amount_yuan", amount, "market_price_volume", "yuan", event_at)
            if _at_or_before("09:45:00", cutoff):
                add("p0945_amount_yuan", amount, "market_price_volume", "yuan", event_at)
                add("p0945_median", median, "market_price_volume", "pp", event_at)
                add("p0945_up_share", float(share) / 100 if isinstance(share, (int, float)) else None,
                    "market_price_volume", "ratio", event_at)
        # Keep compact plate facts in the bundle; downstream rendering can use them as evidence.
        add("plate_top_share", market.get("top_plates_by_share") if valid_market else None, "market_price_volume", "rows", event_at)
        add("market_limit_up_price_count", market.get("limit_up") if valid_market else None, "market_price_volume", "count", event_at)
        add("market_limit_down_price_count", market.get("limit_down") if valid_market else None, "market_price_volume", "count", event_at)
    elif stage != "auction":
        for key in ("auction_amount_yuan", "auction_median_pct", "auction_up_share",
                    "p0945_amount_yuan", "p0945_median_pct", "p0945_up_share",
                    "open_median_pct", "open_up_share", "plate_top_share",
                    "market_limit_up_price_count", "market_limit_down_price_count"):
            add(key, None, "market_price_volume", None, market.get("asof") or cutoff)

    # Live sources may provide the 09:32/09:38/09:44 checkpoints individually.
    # Their own timestamps are retained so a later row cannot leak into an early story.
    snapshots = report.get("snapshots") if isinstance(report.get("snapshots"), list) else []
    for row in snapshots:
        if not isinstance(row, Mapping):
            continue
        if row.get("status") in {"STALE", "NOT_READY", "NOT_YET_AVAILABLE"} or row.get("symbol_count") == 0:
            continue
        label = str(row.get("label") or row.get("tag") or "").replace(":", "")
        stamp = row.get("asof") or row.get("observed_at") or row.get("timestamp")
        if not stamp or not _at_or_before(stamp, cutoff):
            continue
        key = {"0920": "auction_0920", "0924": "auction_0924", "0925": "auction",
               "0932": "p0932", "0938": "p0938", "0944": "p0944", "0945": "p0945"}.get(label)
        if key is None or (stage == "auction" and key.startswith("p")):
            continue
        median = row.get("median_pct_change", row.get("median_pct", row.get("median_pp")))
        share = row.get("up_share_pct", row.get("up_share"))
        if isinstance(share, (int, float)) and share > 1:
            share = share / 100
        amount = row.get("market_match_amount_yuan", row.get("amount_yuan", row.get("cumulative_amount_yuan")))
        for suffix, value, unit in (("median_pct", median, "pp"), ("up_share", share, "ratio"),
                                    ("amount_yuan", amount, "yuan")):
            facts[f"{key}_{suffix}"] = {"value": value, "unit": unit, "event_cutoff": stamp,
                                        "refs": [f"snapshots:{label}"]}

    # The open-stage headline is the first continuous post-auction checkpoint,
    # not whichever later slice happens to be the report's current market row.
    if stage == "open":
        for metric, snapshot_metric in (("open_median_pct", "p0932_median_pct"),
                                       ("open_up_share", "p0932_up_share"),
                                       ("open_amount_yuan", "p0932_amount_yuan")):
            if snapshot_metric in facts:
                facts[metric] = facts[snapshot_metric]

    ladder = report.get("ladder_section") if isinstance(report.get("ladder_section"), Mapping) else {}
    extremes = report.get("extremes_board")
    if not isinstance(extremes, Mapping):
        extremes = chapters.get("extremes_board", {})
    extreme_stage = "auction" if stage == "auction" else "open"
    extreme_slice = extremes.get(extreme_stage, {}) if isinstance(extremes, Mapping) else {}
    if isinstance(extreme_slice, list):
        extreme_rows = extreme_slice
    elif isinstance(extreme_slice, Mapping):
        extreme_rows = extreme_slice.get("rows", [])
    else:
        extreme_rows = []
    quadrants = chapters.get("plate_quadrants", {})
    facts["ladder_rows"] = {"value": _safe_rows(ladder.get("rows", []), stage=stage, cutoff=cutoff), "unit": "rows",
        "refs": ["ladder_section"], "event_cutoff": cutoff}
    facts["extremes_rows"] = {"value": _safe_rows(extreme_rows, stage=stage, cutoff=cutoff),
        "unit": "rows", "refs": ["extremes_board"], "event_cutoff": cutoff}
    facts["plate_rows"] = {"value": _safe_rows(quadrants.get("rows", []) if isinstance(quadrants, Mapping) else [], stage=stage, cutoff=cutoff),
        "unit": "rows", "refs": ["plate_quadrants"], "event_cutoff": quadrants.get("captured_local", cutoff)
        if isinstance(quadrants, Mapping) else cutoff}
    facts["candidate_refs"] = report.get("a_to_b", [])
    facts["source_refs"] = refs
    degradation = report.get("degraded", [])
    quality = ["LIVE_INPUT_DEGRADED"] if degradation else []
    terminal_status = report.get("auction_terminal_0925", {}).get("status") if isinstance(report.get("auction_terminal_0925"), Mapping) else None
    if terminal_status == "PARTIAL":
        quality.append("AUCTION_TERMINAL_PARTIAL")
    if not market or market.get("symbol_count") in (None, 0):
        quality.append("MARKET_SLICE_MISSING")
    elif market.get("status") == "PARTIAL":
        quality.append("MARKET_SLICE_PARTIAL")
    facts["quality"] = quality
    if degradation:
        facts["source_refs"] = refs + ["degraded:" + str(x) for x in degradation[:5]]
    provenance = {"source_fidelity": "LIVE_REPORT", "refs": refs}
    return build_story_features(facts, stage=stage, cutoff=cutoff,
                                observed_at=observed_at, provenance=provenance)
