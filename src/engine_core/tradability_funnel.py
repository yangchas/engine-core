"""A cautious, deterministic A→B observation funnel (not a trade instruction)."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_BUCKET = {"能做": "watch", "要等": "wait", "别碰": "exclude", "watch": "watch", "wait": "wait", "exclude": "exclude"}
_LABEL = {"watch": "能做", "wait": "要等", "exclude": "别碰"}


def filter_candidates(candidates: Sequence[Mapping[str, Any]], features: Mapping[str, Any], *,
                      asof: str, policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    stage = str(features.get("stage", "auction"))
    metrics = features.get("metrics", {}) if isinstance(features.get("metrics"), Mapping) else {}
    rows, counts = {k: [] for k in _LABEL}, {k: 0 for k in _LABEL}
    seen: dict[str, dict[str, Any]] = {}
    for i, source in enumerate(candidates):
        bucket = _BUCKET.get(str(source.get("bucket", source.get("classification", source.get("status", "wait")))) , "wait")
        symbol = str(source.get("symbol", source.get("a_symbol", "")))
        name = str(source.get("name", source.get("a_name", symbol or "未命名")))
        # Explicitly ignore all later-stage result fields in early-stage eligibility and ordering.
        status = str(source.get("price_status", source.get("auction_price_status", "UNKNOWN")))
        position = source.get("position_asof", source.get("position20_prior"))
        if stage in {"auction", "open", "intraday"} and source.get("auction_price_status"):
            status = str(source["auction_price_status"])
        reasons_raw = source.get("reasons", source.get("reason", []))
        if isinstance(reasons_raw, str): reasons_raw = [reasons_raw]
        reasons = [str(x) for x in reasons_raw] if isinstance(reasons_raw, Sequence) else []
        condition = str(source.get("condition") or ("观察同口径下一截点是否延续" if bucket != "exclude" else "仅在价格与位置风险解除后复核"))
        risk = str(source.get("risk") or ("价格/成交可行性未验证" if status in {"UNKNOWN", "INDICATIVE", "PRICE_PROXY"} else "同板块不代表个股联动"))
        if position is None and "position" in source:
            position = source.get("position")
        ref = source.get("origin_refs", source.get("refs", []))
        item = {"symbol": symbol, "name": name, "bucket": bucket,
            "reasons": [{"claim_id": f"candidate:{symbol}:{j}", "text": reason, "refs": list(ref) if isinstance(ref, Sequence) and not isinstance(ref, str) else []}
                        for j, reason in enumerate(reasons[:3])],
            "condition": {"claim_id": f"candidate:{symbol}:condition", "text": condition,
                          "refs": list(ref) if isinstance(ref, Sequence) and not isinstance(ref, str) else []},
            "risk": risk, "origin_refs": list(ref) if isinstance(ref, Sequence) and not isinstance(ref, str) else [],
            "price_status": status, "position_asof": source.get("position_asof"), "asof": asof,
            "origin": str(source.get("origin", "A_TO_B")), "_index": i}
        if symbol and symbol in seen:
            # Same stock reached through multiple themes: one display row, with
            # the more cautious bucket winning and reasons kept as references.
            previous = seen[symbol]
            order = {"watch": 0, "wait": 1, "exclude": 2}
            if order[bucket] > order[previous["bucket"]]:
                rows[previous["bucket"]].remove(previous)
                rows[bucket].append(item)
                seen[symbol] = item
            else:
                previous["reasons"] = (previous["reasons"] + item["reasons"])[:3]
                previous["origin_refs"] = list(dict.fromkeys(previous["origin_refs"] + item["origin_refs"]))
        else:
            rows[bucket].append(item)
            if symbol:
                seen[symbol] = item
    counts = {bucket: len(rows[bucket]) for bucket in _LABEL}
    shown = {}
    for bucket, label in _LABEL.items():
        # Preserve source order; never rank on later outcome fields or synthesize a score.
        shown[bucket] = [{k: v for k, v in row.items() if k != "_index"} for row in rows[bucket][:5]]
    return {"asof": asof, "candidate_origin": "A_TO_B", "counts": counts, "shown": shown,
            "reason_counts": counts.copy(), "all_rows_ref": None, "stage": stage}
