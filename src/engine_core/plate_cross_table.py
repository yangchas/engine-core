"""Same-checkpoint, strategy-free plate comparison product for live reports."""

from __future__ import annotations

import math
from collections.abc import Mapping, Set, Sequence
from typing import Any


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _leader(symbols: Sequence[str], states: Mapping[str, Mapping[str, Any]], field: str,
            *, reverse: bool = True) -> dict[str, Any] | None:
    candidates = []
    for symbol in symbols:
        row = states.get(symbol, {})
        value = _number(row.get(field))
        if value is not None:
            candidates.append((value, symbol, row))
    if not candidates:
        return None
    value, symbol, row = sorted(candidates, key=lambda item: ((-item[0] if reverse else item[0]), item[1]))[0]
    return {"symbol": symbol, "name": row.get("name") or symbol, "value": value}


def build_plate_cross_table(
    *,
    states: Mapping[str, Mapping[str, Any]],
    relations: Mapping[str, Set[str]],
    plate_names: Mapping[str, str] | None = None,
    market_amount_yuan: float | None = None,
    previous_rows: Sequence[Mapping[str, Any]] | None = None,
    promotion_by_theme: Mapping[str, Mapping[str, Any]] | None = None,
    member_counts: Mapping[str, int] | None = None,
    class_by_code: Mapping[str, str] | None = None,
    historical_by_code: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build a compact per-plate table from one membership version/checkpoint.

    Amounts are member sums: an overlapping stock is counted once in every plate
    containing it. The market denominator is the unique whole-market amount.
    Missing stock amounts, yesterday snapshots, and seal times remain unavailable.
    """
    names = plate_names or {}
    promotions = promotion_by_theme or {}
    previous = {str(row.get("code") or row.get("plate_code") or ""): row
                for row in previous_rows or () if isinstance(row, Mapping)}
    sizes = member_counts or {}
    classes = class_by_code or {}
    historical = historical_by_code or {}
    market_total = _number(market_amount_yuan)
    normalized_relations = [(str(raw_code), sorted(set(map(str, raw_members))))
                            for raw_code, raw_members in relations.items()]
    symbol_ids = {symbol: index for index, symbol in enumerate(
        sorted({symbol for _code, members in normalized_relations for symbol in members}))}
    members_by_code = {}
    plates_by_member: dict[str, set[str]] = {}
    for code, members in normalized_relations:
        member_bits = 0
        for symbol in members:
            plates_by_member.setdefault(symbol, set()).add(code)
            member_bits |= 1 << symbol_ids[symbol]
        members_by_code[code] = (len(members), member_bits)
    output = []
    for code, members in normalized_relations:
        facts = [(symbol, states[symbol]) for symbol in members
                 if isinstance(states.get(symbol), Mapping)]
        amounts = [(symbol, row, amount) for symbol, row in facts
                   if (amount := _number(row.get("amount_yuan", row.get("match_amount_yuan")))) is not None
                   and amount >= 0]
        returns = [(symbol, row, pct) for symbol, row in facts
                   if (pct := _number(row.get("pct_change"))) is not None]
        total_amount = sum(amount for _symbol, _row, amount in amounts) if amounts else None
        previous_row = previous.get(code)
        previous_pct = _number(previous_row.get("equal_weight_pct")) if previous_row else None
        current_pct = sum(pct for _symbol, _row, pct in returns) / len(returns) if returns else None
        if previous_row is not None and previous_pct is not None and current_pct is not None:
            yesterday = {"status": "READY", "change_pp": current_pct - previous_pct,
                         "previous_pct": previous_pct}
        else:
            yesterday = {"status": "UNKNOWN", "change_pp": None,
                         "reason": ("NO_PREVIOUS_SAME_CHECKPOINT_DATA" if previous_row is None
                                    else "PREVIOUS_OR_CURRENT_EQUAL_WEIGHT_MISSING")}

        levels = [(symbol, row, level) for symbol, row in facts
                  if (level := _number(row.get("lb_days"))) is not None and level > 0]
        seal_candidates = [(symbol, row, str(row.get("seal_time"))) for symbol, row in facts
                           if row.get("seal_time") not in (None, "")]
        seal_leader = (None if not seal_candidates else
                       {"symbol": min(seal_candidates, key=lambda item: (item[2], item[0]))[0],
                        "name": min(seal_candidates, key=lambda item: (item[2], item[0]))[1].get("name") or
                                min(seal_candidates, key=lambda item: (item[2], item[0]))[0],
                        "seal_time": min(seal_candidates, key=lambda item: (item[2], item[0]))[2]})
        pool_symbols = [symbol for symbol, _row, _level in levels]
        top_board = _leader(pool_symbols, {s: states[s] for s in pool_symbols}, "lb_days")
        reason_sclt = []
        for _symbol, row in facts:
            value = row.get("reason_sclt")
            if value not in (None, "") and str(value) not in reason_sclt:
                reason_sclt.append(str(value))
        promotion = promotions.get(code)
        prior = previous.get(code)
        denominator = int(promotion.get("total", 0)) if promotion else 0
        numerator = int(promotion.get("promoted", 0)) if promotion else 0
        size = sizes.get(code, len(members))
        plate_class = classes.get(code) or ("BROAD" if size >= 300 else "NICHE" if size <= 15 else "MID")
        overlaps = []
        own_count, own_bits = members_by_code[code]
        candidates = set().union(*(plates_by_member[symbol] for symbol in members)) if members else set()
        for other_code in candidates:
            if other_code == code:
                continue
            other_count, other_bits = members_by_code[other_code]
            if min(own_count, other_count) / max(own_count, other_count) < 0.8:
                continue
            intersection = (own_bits & other_bits).bit_count()
            union_count = own_count + other_count - intersection
            jaccard = intersection / union_count if union_count else 0.0
            if jaccard >= 0.8:
                overlaps.append({"code": other_code,
                                 "name": str(names.get(other_code) or other_code),
                                 "jaccard": jaccard})
        hist = historical.get(code, {})
        history_values = [value for item in hist.get("amounts_yuan", ())
                          if (value := _number(item)) is not None]
        history_n = int(hist.get("sample_days", len(history_values)) or 0)
        history_median = _number(hist.get("median_yuan"))
        current_percentile = None
        if total_amount is not None and len(amounts) == len(members) and history_values:
            current_percentile = 100.0 * (sum(value < total_amount for value in history_values) +
                                           0.5 * sum(value == total_amount for value in history_values)) / len(history_values)
        elif hist.get("percentile") is not None:
            current_percentile = _number(hist.get("percentile"))
        history_status = ("UNKNOWN" if history_n == 0 else "样本少" if history_n < 5 else
                          "READY" if history_median is not None and current_percentile is not None else "UNKNOWN")
        output.append({
            "code": code, "name": str(names.get(code) or code),
            "class": plate_class,
            "member_count": size,
            "overlap_with": sorted(overlaps, key=lambda item: (-item["jaccard"], item["code"])),
            "member_count": len(members), "covered_member_count": len(facts),
            "amount_yuan": total_amount, "amount_basis": "MEMBER_SUM_OVERLAP_REPEATED",
            "amount_coverage": {"known": len(amounts), "members": len(members),
                                "ratio": len(amounts) / len(members) if members else None},
            "market_share_pct": (100.0 * total_amount / market_total
                                 if total_amount is not None and market_total is not None and market_total > 0 else None),
            "equal_weight_pct": current_pct,
            "return_coverage": {"known": len(returns), "members": len(members),
                                "ratio": len(returns) / len(members) if members else None},
            "up_count": sum(pct > 0 for _symbol, _row, pct in returns),
            "down_count": sum(pct < 0 for _symbol, _row, pct in returns),
            "flat_count": sum(pct == 0 for _symbol, _row, pct in returns),
            "limit_up_count": sum(row.get("limit_state") == 1 for _symbol, row in facts),
            "limit_down_count": sum(row.get("limit_state") == -1 for _symbol, row in facts),
            "sealed_count": sum(row.get("sealed") is True or row.get("limit_state") == 1
                                for _symbol, row in facts),
            "sealed_unknown_count": sum(row.get("sealed") is None and row.get("limit_state") is None
                                        for _symbol, row in facts) + len(members) - len(facts),
            "highest_board": max((level for _symbol, _row, level in levels), default=None),
            "promotion_rate": _number(promotion.get("promotion_rate")) if promotion else None,
            "promotion_numerator": numerator if promotion else None,
            "promotion_denominator": denominator if promotion else None,
            "promotion_status": ("UNKNOWN" if not promotion else "样本少" if denominator < 3 else
                                 "READY" if _number(promotion.get("promotion_rate")) is not None else "UNKNOWN"),
            "historical_64d_multiple": _number(hist.get("multiple")),
            "historical_64d_percentile": _number(hist.get("percentile")),
            "historical_amount_multiple": (total_amount / history_median
                                           if total_amount is not None and len(amounts) == len(members)
                                           and history_median is not None and history_median > 0
                                           else None),
            "historical_amount_percentile": current_percentile,
            "historical_amount_sample_days": history_n,
            "historical_amount_median_yuan": history_median,
            "historical_amount_status": history_status,
            "historical_amount_slot": hist.get("slot"),
            "historical_amount_version": hist.get("version"),
            "historical_amount_used_date": hist.get("used_date"),
            "yesterday_same_checkpoint": yesterday,
            "leaders": {
                "amount_max": _leader([s for s, _r, _a in amounts], states, "amount_yuan"),
                "highest_board": top_board,
                "earliest_seal": seal_leader,
                "reason_sclt": reason_sclt,
            },
        })
    output.sort(key=lambda row: (row["amount_yuan"] is None,
                                 -(row["amount_yuan"] or 0.0), row["code"]))
    return output


__all__ = ["build_plate_cross_table"]
