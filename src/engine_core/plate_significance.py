"""Robust, point-in-time plate significance checks for one cross-section."""

from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import median
from typing import Collection, Mapping


@dataclass(frozen=True)
class PlateSignificanceResult:
    market: dict[str, float | int]
    plates: list[dict[str, object]]
    controls: dict[str, float]
    missing_count: int


@dataclass(frozen=True)
class IndependentSelectionResult:
    market: dict[str, float | int]
    plates: list[dict[str, object]]
    candidates: list[dict[str, object]]
    controls: dict[str, float]
    uncovered: dict[str, float | int]
    missing_count: int
    rounds: int


def _valid_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _cap_and_normalize(raw: list[float], cap_share: float) -> list[float]:
    """Proportionally cap large weights, redistributing excess to uncapped rows."""
    if not raw:
        return []
    total = sum(raw)
    if total <= 0:
        return [1.0 / len(raw)] * len(raw)
    cap = max(1.0 / len(raw), min(float(cap_share), 1.0))
    shares = [value / total for value in raw]
    fixed: set[int] = set()
    while True:
        over = [i for i, value in enumerate(shares) if i not in fixed and value > cap]
        if not over:
            break
        fixed.update(over)
        left = 1.0 - cap * len(fixed)
        remaining = [i for i in range(len(raw)) if i not in fixed]
        shares = [cap if i in fixed else 0.0 for i in range(len(raw))]
        if not remaining or left <= 0:
            break
        remainder_total = sum(raw[i] for i in remaining)
        for i in remaining:
            shares[i] = left * raw[i] / remainder_total
    norm = sum(shares)
    return [value / norm for value in shares]


def _weighted_trimmed_mean(values: list[float], weights: list[float], trim: float) -> float:
    ordered = sorted(zip(values, weights), key=lambda item: item[0])
    tail = min(max(float(trim), 0.0), 0.49) * sum(weights)
    retained: list[tuple[float, float]] = []
    left = tail
    for value, weight in ordered:
        removed = min(left, weight)
        left -= removed
        keep = weight - removed
        if keep > 0:
            retained.append((value, keep))
    right = tail
    for index in range(len(retained) - 1, -1, -1):
        value, weight = retained[index]
        removed = min(right, weight)
        right -= removed
        retained[index] = (value, weight - removed)
    denominator = sum(weight for _, weight in retained)
    if denominator <= 0:
        return median(values)
    return sum(value * weight for value, weight in retained) / denominator


def _stats(
    symbols: Collection[str],
    returns: Mapping[str, float | None],
    all_weights: Mapping[str, float] | None,
    market_median: float,
    market_scale: float,
    *,
    trim: float,
    cap_share: float,
    limit_up_symbols: Collection[str],
) -> dict[str, object]:
    valid = sorted({symbol for symbol in symbols if _valid_number(returns.get(symbol))})
    n = len(valid)
    raw_weights: list[float] = []
    for symbol in valid:
        candidate = all_weights.get(symbol, 1.0) if all_weights is not None else 1.0
        raw_weights.append(float(candidate) if _valid_number(candidate) and candidate > 0 else 1.0)
    normalized = _cap_and_normalize(raw_weights, cap_share)
    neff_denominator = sum(weight * weight for weight in normalized)
    n_eff = 1.0 / neff_denominator if neff_denominator else 0.0
    values = [float(returns[symbol]) for symbol in valid]  # type: ignore[arg-type]
    trimmed_mean = _weighted_trimmed_mean(values, normalized, trim) if valid else 0.0
    delta = trimmed_mean - market_median
    standard_error = market_scale / math.sqrt(n_eff) if n_eff and market_scale else 0.0
    if standard_error > 0:
        z = delta / standard_error
    elif delta == 0:
        z = 0.0
    else:
        z = math.copysign(math.inf, delta)
    largest = max(normalized, default=0.0)
    limit_count = sum(symbol in limit_up_symbols for symbol in valid)
    return {
        "n": n,
        "trim_mean": trimmed_mean,
        "z": z,
        "limit_up_count": limit_count,
        "top1_weight_share": largest,
    }


def _stats_ordered(
    ordered_symbols: list[str],
    returns: Mapping[str, float | None],
    all_weights: Mapping[str, float] | None,
    market_median: float,
    market_scale: float,
    *,
    trim: float,
    cap_share: float,
    limit_up_symbols: Collection[str],
) -> dict[str, object]:
    """Compute the same statistics when symbols are already return-sorted."""
    n = len(ordered_symbols)
    if all_weights is None:
        n_eff = float(n)
        top1 = 1.0 / n if n else 0.0
        trim_share = min(max(float(trim), 0.0), 0.49)
        tail_count = trim_share * n
        whole_tail = int(tail_count)
        fraction = tail_count - whole_tail
        low = whole_tail
        high = n - whole_tail
        retained_count = n - 2 * tail_count
        if high - low <= 1:
            trimmed_mean = float(returns[ordered_symbols[low]]) if n else 0.0  # type: ignore[arg-type]
        elif fraction == 0:
            trimmed_mean = sum(float(returns[symbol]) for symbol in ordered_symbols[low:high]) / retained_count  # type: ignore[arg-type]
        else:
            middle = sum(float(returns[symbol]) for symbol in ordered_symbols[low + 1:high - 1])  # type: ignore[arg-type]
            middle += (1.0 - fraction) * (
                float(returns[ordered_symbols[low]]) + float(returns[ordered_symbols[high - 1]])
            )  # type: ignore[arg-type]
            trimmed_mean = middle / retained_count
        limit_count = sum(symbol in limit_up_symbols for symbol in ordered_symbols) if limit_up_symbols else 0
    else:
        raw_weights: list[float] = []
        for symbol in ordered_symbols:
            candidate = all_weights.get(symbol, 1.0)
            raw_weights.append(float(candidate) if _valid_number(candidate) and candidate > 0 else 1.0)
        normalized = _cap_and_normalize(raw_weights, cap_share)
        neff_denominator = sum(weight * weight for weight in normalized)
        n_eff = 1.0 / neff_denominator if neff_denominator else 0.0
        total_weight = sum(normalized)
        tail = min(max(float(trim), 0.0), 0.49) * total_weight
        denominator = total_weight - 2 * tail
        cumulative = 0.0
        trimmed_total = 0.0
        for symbol, weight in zip(ordered_symbols, normalized):
            next_cumulative = cumulative + weight
            retained = max(0.0, min(next_cumulative, total_weight - tail) - max(cumulative, tail))
            if retained:
                trimmed_total += float(returns[symbol]) * retained  # type: ignore[arg-type]
            cumulative = next_cumulative
        trimmed_mean = trimmed_total / denominator if denominator > 0 else 0.0
        top1 = max(normalized, default=0.0)
        limit_count = sum(symbol in limit_up_symbols for symbol in ordered_symbols) if limit_up_symbols else 0
    delta = trimmed_mean - market_median
    standard_error = market_scale / math.sqrt(n_eff) if n_eff and market_scale else 0.0
    z = delta / standard_error if standard_error > 0 else (0.0 if delta == 0 else math.copysign(math.inf, delta))
    return {
        "n": n,
        "trim_mean": trimmed_mean,
        "z": z,
        "limit_up_count": limit_count,
        "top1_weight_share": top1,
    }


def _p_value(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2.0))


def _bh(rows: list[dict[str, object]], fdr: float) -> None:
    ordered = sorted(rows, key=lambda row: (float(row["p"]), str(row["code"])))
    count = len(ordered)
    running = 1.0
    for rank_index in range(count - 1, -1, -1):
        row = ordered[rank_index]
        rank = rank_index + 1
        running = min(running, float(row["p"]) * count / rank)
        row["q"] = min(1.0, running)
    for row in rows:
        z = float(row["z"])
        if float(row["q"]) <= fdr:
            row["status"] = "SIGNIFICANT_STRONG" if z > 0 else "SIGNIFICANT_WEAK"
        else:
            row["status"] = "NO_SIGNAL"


def detect_significant_plates(
    returns: Mapping[str, float | None],
    members_by_plate: Mapping[str, Collection[str]],
    registry: Mapping[str, Mapping[str, object]],
    *,
    weights: Mapping[str, float] | None = None,
    fdr: float = 0.10,
    min_n: int = 5,
    trim: float = 0.1,
    weight_cap_share: float = 0.25,
    limit_up_symbols: Collection[str] = (),
) -> PlateSignificanceResult:
    """Test per-plate cross-sectional strength with robust scale and BH FDR."""
    observed = [float(value) for value in returns.values() if _valid_number(value)]
    market_median = median(observed) if observed else 0.0
    mad = median([abs(value - market_median) for value in observed]) if observed else 0.0
    market_scale = 1.4826 * mad
    all_symbols = set(returns)
    for members in members_by_plate.values():
        all_symbols.update(members)
    missing_count = sum(not _valid_number(returns.get(symbol)) for symbol in all_symbols)
    market: dict[str, float | int] = {
        "median": market_median,
        "mad_scale": market_scale,
        "n": len(observed),
    }

    allowed = {"THEME", "NICHE", "BROAD"}
    rows: dict[str, dict[str, object]] = {}
    control_z: dict[str, float] = {}
    for code in sorted(members_by_plate):
        metadata = registry.get(code, {})
        plate_class = str(metadata.get("class", ""))
        stats = _stats(
            members_by_plate[code], returns, weights, market_median, market_scale,
            trim=trim, cap_share=weight_cap_share, limit_up_symbols=limit_up_symbols,
        )
        if plate_class in {"GENERIC_FACTOR", "EVENT"}:
            control_z[code] = float(stats["z"])
            continue
        if plate_class not in allowed:
            continue
        name = str(metadata.get("name", code))
        row: dict[str, object] = {
            "code": code,
            "name": name,
            "class": plate_class,
            **stats,
            "p": _p_value(float(stats["z"])),
            "q": 1.0,
            "status": "TOO_SMALL" if int(stats["n"]) < min_n else "NO_SIGNAL",
            "residual_n": None,
            "residual_z": None,
            "significant_children": 0,
            "folded_into": None,
        }
        rows[code] = row

    # Determine significant children first so broad parents can be evaluated on
    # their remaining members. The final BH below is run across all test rows.
    child_candidates = [row for row in rows.values() if row["class"] != "BROAD" and int(row["n"]) >= min_n]
    _bh(child_candidates, fdr)
    significant_codes = {
        str(row["code"]) for row in child_candidates
        if str(row["status"]).startswith("SIGNIFICANT_")
    }
    for code, row in rows.items():
        if row["class"] != "BROAD":
            continue
        metadata = registry.get(code, {})
        children = sorted(str(child) for child in (metadata.get("children") or []))
        significant_children = [child for child in children if child in significant_codes]
        if not children:
            continue
        excluded: set[str] = set()
        for child in significant_children:
            excluded.update(members_by_plate.get(child, ()))
        residual_symbols = set(members_by_plate.get(code, ())) - excluded
        residual = _stats(
            residual_symbols, returns, weights, market_median, market_scale,
            trim=trim, cap_share=weight_cap_share, limit_up_symbols=limit_up_symbols,
        )
        row["significant_children"] = len(significant_children)
        row["residual_n"] = residual["n"]
        row["residual_z"] = residual["z"]
        row.update(residual)
        row["p"] = _p_value(float(residual["z"]))
        row["status"] = "TOO_SMALL" if int(residual["n"]) < min_n else "NO_SIGNAL"

    test_rows = [row for row in rows.values() if int(row["n"]) >= min_n]
    _bh(test_rows, fdr)

    significant_by_family: dict[str, list[dict[str, object]]] = {}
    for row in rows.values():
        if str(row["status"]).startswith("SIGNIFICANT_"):
            family = str(registry.get(str(row["code"]), {}).get("family") or row["code"])
            significant_by_family.setdefault(family, []).append(row)
    for family_rows in significant_by_family.values():
        winner = min(family_rows, key=lambda row: (-abs(float(row["z"])), str(row["code"])))
        for row in family_rows:
            if row is not winner:
                row["folded_into"] = winner["code"]

    output = sorted(rows.values(), key=lambda row: (-float(row["z"]), str(row["code"])))
    return PlateSignificanceResult(market=market, plates=output, controls=control_z, missing_count=missing_count)


def _quantile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def select_independent_plates(
    returns: Mapping[str, float | None],
    members_by_plate: Mapping[str, Collection[str]],
    registry: Mapping[str, Mapping[str, object]],
    *,
    weights: Mapping[str, float] | None = None,
    fdr: float = 0.10,
    min_n: int = 5,
    trim: float = 0.1,
    weight_cap_share: float = 0.25,
    max_selected: int = 20,
    min_residual_share: float = 0.4,
    min_residual_n: int = 8,
    limit_up_symbols: Collection[str] = (),
) -> IndependentSelectionResult:
    """Greedily select significant plates by testing only unexplained members."""
    observed = [float(value) for value in returns.values() if _valid_number(value)]
    market_median = median(observed) if observed else 0.0
    mad = median([abs(value - market_median) for value in observed]) if observed else 0.0
    market_scale = 1.4826 * mad
    market: dict[str, float | int] = {
        "median": market_median,
        "mad_scale": market_scale,
        "n": len(observed),
    }

    allowed = {"THEME", "NICHE", "BROAD"}
    all_symbols = set(returns)
    for members in members_by_plate.values():
        all_symbols.update(members)
    missing_count = sum(not _valid_number(returns.get(symbol)) for symbol in all_symbols)
    candidate_members: dict[str, set[str]] = {}
    ordered_members: dict[str, list[str]] = {}
    candidates: dict[str, dict[str, object]] = {}
    control_z: dict[str, float] = {}
    for code in sorted(members_by_plate):
        metadata = registry.get(code, {})
        plate_class = str(metadata.get("class", ""))
        if plate_class not in allowed and plate_class not in {"GENERIC_FACTOR", "EVENT"}:
            continue
        valid_members = {
            symbol for symbol in members_by_plate[code]
            if _valid_number(returns.get(symbol))
        }
        ordered = sorted(valid_members, key=lambda symbol: (float(returns[symbol]), symbol))  # type: ignore[arg-type]
        stats = _stats_ordered(
            ordered, returns, weights, market_median, market_scale,
            trim=trim, cap_share=weight_cap_share, limit_up_symbols=limit_up_symbols,
        )
        if plate_class in {"GENERIC_FACTOR", "EVENT"}:
            control_z[code] = float(stats["z"])
            continue
        candidate_members[code] = valid_members
        ordered_members[code] = ordered
        candidates[code] = {
            "code": code,
            "name": str(metadata.get("name", code)),
            "class": plate_class,
            **stats,
            "residual_n": int(stats["n"]),
            "q": 1.0,
            "status": "TOO_SMALL" if int(stats["n"]) < min_n else "NO_SIGNAL",
            "covered_by": None,
        }

    selected: list[dict[str, object]] = []
    explained: set[str] = set()
    rounds = 0
    while len(selected) < max(0, max_selected):
        rounds += 1
        eligible: list[dict[str, object]] = []
        for code in sorted(candidates):
            row = candidates[code]
            if row["status"] == "SELECTED" or row["status"] == "REDUNDANT":
                continue
            original_n = int(row["n"])
            if original_n < min_n:
                row["status"] = "TOO_SMALL"
                continue
            residual_ordered = [symbol for symbol in ordered_members[code] if symbol not in explained]
            residual_n = len(residual_ordered)
            threshold = max(min_residual_n, min_residual_share * original_n)
            below_minimum = residual_n < min_n
            below_residual_floor = bool(selected) and residual_n < threshold
            if below_minimum or below_residual_floor:
                overlap_counts = [
                    (len(candidate_members[code] & candidate_members[selected_code]), selected_code)
                    for selected_code in (str(item["code"]) for item in selected)
                ]
                row["residual_n"] = residual_n
                row["status"] = "REDUNDANT" if selected else "TOO_SMALL"
                row["covered_by"] = min(overlap_counts, key=lambda item: (-item[0], item[1]))[1] if overlap_counts else None
                continue
            stats = _stats_ordered(
                residual_ordered, returns, weights, market_median, market_scale,
                trim=trim, cap_share=weight_cap_share, limit_up_symbols=limit_up_symbols,
            )
            row.update({
                "residual_n": residual_n,
                "trim_mean": stats["trim_mean"],
                "z": stats["z"],
                "limit_up_count": stats["limit_up_count"],
                "top1_weight_share": stats["top1_weight_share"],
                "p": _p_value(float(stats["z"])),
                "q": 1.0,
                "status": "NO_SIGNAL",
                "covered_by": None,
            })
            if residual_n >= min_n:
                eligible.append(row)

        if not eligible:
            break
        _bh(eligible, fdr)
        significant = [row for row in eligible if str(row["status"]).startswith("SIGNIFICANT_")]
        if not significant:
            break
        winner = min(significant, key=lambda row: (-abs(float(row["z"])), str(row["code"])))
        code = str(winner["code"])
        winner["rank"] = len(selected) + 1
        winner["direction"] = "STRONG" if float(winner["z"]) > 0 else "WEAK"
        winner["explained_members"] = len(candidate_members[code] - explained)
        winner["status"] = "SELECTED"
        selected.append(winner.copy())
        explained.update(candidate_members[code])

    uncovered_values = [
        float(returns[symbol]) for symbol in sorted(set(returns) - explained)
        if _valid_number(returns.get(symbol))
    ]
    uncovered: dict[str, float | int] = {
        "n": len(uncovered_values),
        "median": median(uncovered_values) if uncovered_values else 0.0,
        "trim_mean": _weighted_trimmed_mean(uncovered_values, [1.0] * len(uncovered_values), trim)
        if uncovered_values else 0.0,
        "p10": _quantile(uncovered_values, 0.10),
        "p90": _quantile(uncovered_values, 0.90),
    }
    ordered_candidates = [candidates[code] for code in sorted(candidates)]
    return IndependentSelectionResult(
        market=market,
        plates=selected,
        candidates=ordered_candidates,
        controls=control_z,
        uncovered=uncovered,
        missing_count=missing_count,
        rounds=rounds,
    )
