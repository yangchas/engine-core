"""Descriptive per-plate linkage and sealed-limit concentration metrics."""
from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Set
from typing import Any


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _ratio(numerator: float, denominator: float) -> float | None:
    return 100.0 * numerator / denominator if denominator > 0 else None


def build_plate_linkage(states: Mapping[str, Mapping[str, Any]],
                        relations: Mapping[str, Set[str]],
                        plate_names: Mapping[str, str] | None = None, *,
                        stock_names: Mapping[str, str] | None = None,
                        generic_names: Set[str] | None = None,
                        min_members: int = 8, top_n: int | None = 15) -> dict[str, Any]:
    """Aggregate supplied stock slices; absent returns and amounts remain unavailable.

    The member threshold counts priced members, matching live plate eligibility. Amount
    ranks and amount ratios use only explicitly supplied finite amounts; price weighted
    metrics additionally require a finite return.
    """
    plate_names, stock_names = plate_names or {}, stock_names or {}
    generic_names = generic_names or set()
    result = []
    for code, members in relations.items():
        name = plate_names.get(code, code)
        if name in generic_names:
            continue
        rows = [(symbol, states[symbol]) for symbol in members if symbol in states]
        from engine_core.auction_tracking import _at_limit_pair
        sealed = {}
        for symbol, row in rows:
            state = row.get("limit_state")
            if state in (-1, 0, 1):
                sealed[symbol] = (state == 1, state == -1)
            else:
                try:
                    sealed[symbol] = _at_limit_pair(row)
                except (KeyError, TypeError, ValueError, OverflowError):
                    sealed[symbol] = (None, None)
        facts = [(symbol, row, _finite(row.get("pct_change")),
                  _finite(row.get("match_amount_yuan"))) for symbol, row in rows]
        priced = [(symbol, row, pct) for symbol, row, pct, _amount in facts if pct is not None]
        if len(priced) < min_members:
            continue
        known_amounts = [(symbol, row, amount) for symbol, row, _pct, amount in facts
                         if amount is not None and amount >= 0]
        amount_total = sum(amount for _, _, amount in known_amounts)
        priced_amount_rows = [(symbol, row, amount, pct) for symbol, row, pct, amount in facts
                              if amount is not None and amount >= 0 and pct is not None]
        weighted_denominator = sum(amount for _, _, amount, _ in priced_amount_rows)
        weighted_contribution = sum(amount * pct for _, _, amount, pct in priced_amount_rows)
        equal_mean = statistics.mean(pct for _, _, pct in priced)
        weighted_pct = (weighted_contribution / weighted_denominator
                        if weighted_denominator > 0 else None)

        up_rows = [(symbol, row, amount) for symbol, row, amount in known_amounts
                   if sealed[symbol][0] is True]
        down_rows = [(symbol, row, amount) for symbol, row, amount in known_amounts
                     if sealed[symbol][1] is True]
        up_known = sum(amount for _, _, amount in up_rows)
        down_known = sum(amount for _, _, amount in down_rows)
        up_unknown = sum(sealed[symbol][0] is None for symbol, _ in rows)
        down_unknown = sum(sealed[symbol][1] is None for symbol, _ in rows)

        top_priced = sorted(priced_amount_rows, key=lambda item: (-item[2], item[0]))[:3]
        top3_contribution = sum(amount * pct for _, _, amount, pct in top_priced)
        # 贡献占比可为负分母(板块下跌): 同号表示领涨/领跌股集中, 不因分母<0 丢弃
        leading_share = (100.0 * top3_contribution / weighted_contribution
                         if (weighted_contribution != 0 and top_priced and len(known_amounts) == len(rows)
                             and len(priced_amount_rows) == len(rows)) else None)

        def sealed_top(rows):
            return [{"symbol": symbol, "name": stock_names.get(symbol, row.get("name") or symbol),
                     "amount_yuan": amount}
                    for symbol, row, amount in sorted(rows, key=lambda item: (-item[2], item[0]))[:3]]

        result.append({
            "code": code, "name": name,
            "member_count": len(rows), "priced_member_count": len(priced),
            "amount_member_count": len(known_amounts),
            "weighted_member_count": len(priced_amount_rows),
            "amount_yuan": amount_total if len(known_amounts) else None,
            "up_share_pct": _ratio(sum(pct > 0 for _, _, pct in priced), len(priced)),
            "equal_weight_median_pct": statistics.median(pct for _, _, pct in priced),
            "equal_weight_mean_pct": equal_mean,
            "amount_weighted_pct": weighted_pct,
            "weighted_minus_equal_mean_pp": weighted_pct - equal_mean if weighted_pct is not None else None,
            "limit_up_count": sum(sealed[symbol][0] is True for symbol, _ in rows),
            "limit_up_unknown_count": up_unknown,
            "limit_up_unknown_coverage_pct": _ratio(up_unknown, len(rows)),
            "limit_up_amount_share_pct": (_ratio(up_known, amount_total) if up_unknown == 0 else None),
            "limit_up_top3": sealed_top(up_rows),
            "limit_down_count": sum(sealed[symbol][1] is True for symbol, _ in rows),
            "limit_down_unknown_count": down_unknown,
            "limit_down_unknown_coverage_pct": _ratio(down_unknown, len(rows)),
            "limit_down_amount_share_pct": (_ratio(down_known, amount_total) if down_unknown == 0 else None),
            "top3_leading_contribution_share_pct": leading_share,
        })
    result.sort(key=lambda row: (-(row["amount_yuan"] if row["amount_yuan"] is not None else -1), row["code"]))
    down_top5 = sorted((row for row in result if row["limit_down_count"] > 0),
                       key=lambda row: (-row["limit_down_count"],
                                        -(row["amount_yuan"] if row["amount_yuan"] is not None else -1),
                                        row["code"]))[:5]
    return {"available": bool(result), "rows": result[:top_n] if top_n is not None else result,
            "down_limit_top5": down_top5,
            "method_note": ("成员上涨占比/中位涨幅仅计有涨幅成员；等权均值及额加权涨幅用于(b)，"
                            "额加权仅计同时有额和涨幅成员；封板额占比以已知成员额为分母；"
                            "领涨贡献为额最大的3只有效成员(额×涨幅)之和占全部有效成员贡献。缺失不补0。")}
