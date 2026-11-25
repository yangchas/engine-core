"""Describe plates enriched for newly emerging stocks at an opening snapshot."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Mapping

import numpy as np


_GENERIC_PATH = Path(__file__).resolve().parents[2] / "examples/data/plate_generic_factor_names.json"


def hypergeometric_upper_tail(N: int, n: int, K: int, k: int) -> float:
    """Return P[X >= k] for X~Hypergeom(N, n, K), using log combinations."""
    if N < 0 or n < 0 or K < 0 or k < 0 or n > N or K > N:
        raise ValueError("invalid hypergeometric counts")
    lower = max(k, K - (N - n))
    upper = min(n, K)
    if lower > upper:
        return 0.0
    if lower == 0 and upper == min(n, K):
        return 1.0

    def log_choose(total: int, selected: int) -> float:
        return math.lgamma(total + 1) - math.lgamma(selected + 1) - math.lgamma(total - selected + 1)

    denominator = log_choose(N, K)
    terms = [log_choose(n, j) + log_choose(N - n, K - j) - denominator
             for j in range(lower, upper + 1)]
    peak = max(terms)
    probability = math.exp(peak) * sum(math.exp(value - peak) for value in terms)
    return min(1.0, max(0.0, probability))


def benjamini_hochberg(p_values: list[float]) -> list[float]:
    """Return BH adjusted q-values in the input order."""
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in p_values):
        raise ValueError("p-values must be finite and between zero and one")
    count = len(p_values)
    if not count:
        return []
    order = sorted(range(count), key=lambda i: (p_values[i], i))
    adjusted = [1.0] * count
    running = 1.0
    for rank_index in range(count - 1, -1, -1):
        index = order[rank_index]
        rank = rank_index + 1
        running = min(running, p_values[index] * count / rank)
        adjusted[index] = min(1.0, running)
    return adjusted


def _pct(row: Mapping) -> float | None:
    value = row.get("pct_change")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    price = row.get("price_milli", row.get("px_milli"))
    if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
        return None
    return float(value)


def new_emerging_symbols(auction_states: Mapping, current_states: Mapping) -> list[dict]:
    """Select observed final-auction stocks that rose at least 2pp after auction."""
    result = []
    for raw_symbol, auction in auction_states.items():
        symbol = str(raw_symbol)
        current = current_states.get(raw_symbol, current_states.get(symbol))
        if not isinstance(auction, Mapping) or not isinstance(current, Mapping):
            continue
        auction_pct, current_pct = _pct(auction), _pct(current)
        if auction_pct is None or current_pct is None:
            continue
        delta = current_pct - auction_pct
        if auction_pct < 3 and current_pct >= 0 and delta >= 2:
            result.append({"symbol": symbol,
                           "name": current.get("name") or auction.get("name") or symbol,
                           "auction_pct": auction_pct, "current_pct": current_pct,
                           "change_after_auction_pp": delta,
                           "auction_ts": auction.get("ts"), "current_ts": current.get("ts")})
    return sorted(result, key=lambda row: row["symbol"])


def _generic_names(values) -> set[str]:
    if values is None:
        if not _GENERIC_PATH.exists():
            return set()
        values = json.loads(_GENERIC_PATH.read_text(encoding="utf-8"))
    if isinstance(values, Mapping):
        values = values.get("names", [])
    return {str(value) for value in values}


def infer_plate_enrichment(auction_states: Mapping, current_states: Mapping,
                           relations: Mapping[str, set[str]], *, plate_names=None,
                           stock_names=None, generic_names=None,
                           member_snapshot_date="UNKNOWN") -> dict:
    """Test emerging-stock overlap against every eligible non-generic plate."""
    plate_names, stock_names = plate_names or {}, stock_names or {}
    generics = _generic_names(generic_names)
    codes = sorted(str(code) for code in relations)
    members_by_code = {str(code): {str(symbol) for symbol in members}
                       for code, members in relations.items()}
    relation_symbols = set().union(*(members_by_code.values())) if members_by_code else set()
    eligible = []
    for symbol in sorted(relation_symbols):
        auction = auction_states.get(symbol)
        current = current_states.get(symbol)
        if isinstance(auction, Mapping) and isinstance(current, Mapping):
            if _pct(auction) is not None and _pct(current) is not None:
                eligible.append(symbol)
    N = len(eligible)
    emerging = new_emerging_symbols(auction_states, current_states)
    eligible_set = set(eligible)
    emerging_by_symbol = {row["symbol"]: row for row in emerging if row["symbol"] in eligible_set}
    K = len(emerging_by_symbol)
    summary = {"status": "", "emerging_count": K, "N": N, "member_snapshot_date": member_snapshot_date,
               "plates": [], "method_note": "仅称关联不称买点"}
    if K == 0:
        summary["status"] = "无显著关联板块；新冒头集合大小 0"
        return summary
    if K < 3:
        summary["status"] = f"集合过小不检验；新冒头集合大小 {K}"
        return summary
    if N == 0:
        summary["status"] = f"无显著关联板块；新冒头集合大小 {K}"
        return summary

    stock_index = {symbol: index for index, symbol in enumerate(eligible)}
    emerging_mask = np.asarray([symbol in emerging_by_symbol for symbol in eligible], dtype=bool)
    matrix = np.zeros((len(codes), N), dtype=np.uint8)
    test_rows = []
    for plate_index, code in enumerate(codes):
        plate_name = str(plate_names.get(code, code))
        indices = [stock_index[symbol] for symbol in members_by_code[code] if symbol in stock_index]
        if indices:
            matrix[plate_index, indices] = 1
        n = int(matrix[plate_index].sum())
        if n < 5 or plate_name in generics:
            continue
        k = int(np.count_nonzero(matrix[plate_index].astype(bool) & emerging_mask))
        hit_symbols = [symbol for index, symbol in enumerate(eligible)
                       if matrix[plate_index, index] and emerging_mask[index]]
        test_rows.append({"code": code, "name": plate_name, "N": N, "n": n, "K": K, "intersection": k,
                          "p": hypergeometric_upper_tail(N, n, K, k),
                          "_hit_symbols": sorted(hit_symbols)})
    q_values = benjamini_hochberg([row["p"] for row in test_rows])
    for row, q_value in zip(test_rows, q_values):
        row["q"] = q_value
        if row["intersection"] < 3 or q_value > 0.05:
            continue
        row["members"] = [{"symbol": symbol,
                           "name": stock_names.get(symbol, emerging_by_symbol[symbol]["name"]),
                           "current_pct": emerging_by_symbol[symbol]["current_pct"]}
                          for symbol in row.pop("_hit_symbols")[:8]]
        row["member_snapshot_date"] = member_snapshot_date
        row["note"] = "仅称关联不称买点"
        summary["plates"].append(row)
    summary["plates"].sort(key=lambda row: (row["p"], row["code"]))
    summary["status"] = (f"仅称关联不称买点；新冒头集合大小 {K}" if summary["plates"] else
                         f"无显著关联板块；新冒头集合大小 {K}")
    return summary
