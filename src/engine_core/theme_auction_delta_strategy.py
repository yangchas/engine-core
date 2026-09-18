"""The first deliberately small Auction Shadow rule.

This is an exact, pure extraction of the deployed ``engine-next``
``_infer_snapshot_delta_signal`` rule.  It consumes only the explicit legacy
compatibility numeric fact; it does not read Redis/TD, infer missing values,
emit effects, or claim that the labels are the final Core strategy contract.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

from .contracts import (
    EVIDENCE_HASH_CONTRACT_VERSION,
    SEMANTIC_HASH_CONTRACT_VERSION,
    deep_freeze,
    evidence_hash,
    semantic_hash,
)


LEGACY_THEME_AUCTION_DELTA_STRATEGY_CONTRACT_VERSION = "LegacyThemeAuctionDeltaStrategyV1"
LEGACY_THEME_AUCTION_DELTA_FUNCTION_ID = "theme_auction_delta_compat"


def infer_legacy_theme_delta_signal(
    *,
    amount_delta_24_25: float,
    bid_amount_delta_24_25: float,
    change_pct_delta_avg: float,
    amount_ratio_avg: float,
) -> str:
    """Return the legacy signal using the original precedence and thresholds.

    The caller must provide numeric values from the compatibility fact.  This
    function intentionally does not coerce missing values to zero; the
    compatibility builder owns that historical behavior before this rule is
    called.
    """

    values = {
        "amount_delta_24_25": amount_delta_24_25,
        "bid_amount_delta_24_25": bid_amount_delta_24_25,
        "change_pct_delta_avg": change_pct_delta_avg,
        "amount_ratio_avg": amount_ratio_avg,
    }
    for field_name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(field_name + " must be finite")

    # Keep the deployed order exactly: strong/retracing first, then bid,
    # cooling, mild expansion, and the fallback label.
    if amount_delta_24_25 >= 50_000_000 and change_pct_delta_avg >= 1.0:
        return "增量转强"
    if amount_delta_24_25 >= 50_000_000 and change_pct_delta_avg <= -2.0:
        return "放量回落"
    if bid_amount_delta_24_25 >= 10_000_000 and amount_delta_24_25 >= 0:
        return "封单增强"
    if amount_delta_24_25 <= -20_000_000:
        return "竞价降温"
    if amount_ratio_avg >= 1.5 and amount_delta_24_25 > 0:
        return "温和放量"
    return "平稳"


def build_legacy_theme_delta_shadow_trace(
    facts: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Build a deterministic fact-only trace from compatibility fact mappings.

    The mappings are the frozen ``DataResult.data['facts']`` representation.
    Signals are recomputed from the numeric fields rather than trusted from
    the payload; if a caller includes a signal, it must agree.  This keeps the
    Engine integration a composition boundary and prevents a provider from
    smuggling a different strategy result into the trace.
    """

    if isinstance(facts, (str, bytes, bytearray, Mapping)):
        raise TypeError("facts must be an ordered iterable of mappings")
    semantic_facts: list[dict[str, Any]] = []
    evidence_facts: list[dict[str, Any]] = []
    evidence_refs: set[str] = set()
    seen_themes: set[str] = set()
    for index, raw_fact in enumerate(facts):
        if not isinstance(raw_fact, Mapping):
            raise TypeError(f"fact {index} must be a mapping")
        theme_id = raw_fact.get("theme_id")
        if not isinstance(theme_id, str) or not theme_id.strip():
            raise ValueError(f"fact {index} theme_id is required")
        theme_id = theme_id.strip()
        if theme_id in seen_themes:
            raise ValueError("duplicate theme_id: " + theme_id)
        seen_themes.add(theme_id)
        values: dict[str, Any] = {}
        for field_name in (
            "symbol_count",
            "amount_0925",
            "amount_delta_24_25",
            "amount_ratio_avg",
            "bid_amount_delta_24_25",
            "change_pct_delta_avg",
            "positive_delta_count",
        ):
            value = raw_fact.get(field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"fact {theme_id} {field_name} must be finite")
            values[field_name] = value
        signal = infer_legacy_theme_delta_signal(
            amount_delta_24_25=values["amount_delta_24_25"],
            bid_amount_delta_24_25=values["bid_amount_delta_24_25"],
            change_pct_delta_avg=values["change_pct_delta_avg"],
            amount_ratio_avg=values["amount_ratio_avg"],
        )
        supplied_signal = raw_fact.get("signal")
        if supplied_signal is not None and supplied_signal != signal:
            raise ValueError(f"fact {theme_id} signal does not match numeric fields")
        semantic_facts.append({"theme_id": theme_id, **values, "signal": signal})
        refs = raw_fact.get("evidence_refs", ())
        if isinstance(refs, (str, bytes, bytearray)):
            raise TypeError(f"fact {theme_id} evidence_refs must be ordered")
        normalized_ref_values = []
        for ref in refs:
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError(f"fact {theme_id} evidence_refs must contain non-empty strings")
            normalized_ref_values.append(ref.strip())
        normalized_refs = tuple(sorted(set(normalized_ref_values)))
        evidence_refs.update(normalized_refs)
        evidence_facts.append(
            {
                "theme_id": theme_id,
                "content_hash": raw_fact.get("content_hash"),
                "evidence_hash": raw_fact.get("evidence_hash"),
                "evidence_refs": normalized_refs,
            }
        )
    semantic_facts.sort(key=lambda item: item["theme_id"])
    evidence_facts.sort(key=lambda item: item["theme_id"])
    semantic_payload = {
        "contract_version": LEGACY_THEME_AUCTION_DELTA_STRATEGY_CONTRACT_VERSION,
        "facts": tuple(semantic_facts),
        "hash_contract_version": SEMANTIC_HASH_CONTRACT_VERSION,
    }
    evidence_payload = {
        "contract_version": LEGACY_THEME_AUCTION_DELTA_STRATEGY_CONTRACT_VERSION,
        "facts": tuple(evidence_facts),
        "hash_contract_version": EVIDENCE_HASH_CONTRACT_VERSION,
    }
    signal_counts: dict[str, int] = {}
    for fact in semantic_facts:
        signal = fact["signal"]
        signal_counts[signal] = signal_counts.get(signal, 0) + 1
    trace = {
        "shadow_kind": "LEGACY_THEME_DELTA_SHADOW_V1",
        "decision_status": "FACT_ONLY",
        "status": "OBSERVED" if semantic_facts else "UNAVAILABLE",
        "contract_version": LEGACY_THEME_AUCTION_DELTA_STRATEGY_CONTRACT_VERSION,
        "hash_contract_versions": {
            "semantic": SEMANTIC_HASH_CONTRACT_VERSION,
            "evidence": EVIDENCE_HASH_CONTRACT_VERSION,
        },
        "fact_count": len(semantic_facts),
        "signal_counts": dict(sorted(signal_counts.items())),
        "facts": tuple(semantic_facts),
        "evidence_refs": tuple(sorted(evidence_refs)),
        "content_hash": semantic_hash(semantic_payload),
        "evidence_hash": evidence_hash(evidence_payload),
    }
    return deep_freeze(trace)


__all__ = [
    "LEGACY_THEME_AUCTION_DELTA_STRATEGY_CONTRACT_VERSION",
    "LEGACY_THEME_AUCTION_DELTA_FUNCTION_ID",
    "build_legacy_theme_delta_shadow_trace",
    "infer_legacy_theme_delta_signal",
]
