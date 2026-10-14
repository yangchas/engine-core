"""Sparse, strategy-free stock/plate auction strength facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np


@dataclass(frozen=True)
class Incidence:
    symbols: tuple[str, ...]
    plates: tuple[str, ...]
    rows: np.ndarray
    cols: np.ndarray
    plate_meta: Mapping[str, Mapping[str, Any]]
    dropped_plates: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlateStrength:
    plate: str
    n_members: int
    n_eff: float
    coverage: float
    excess_amt_weighted: float | None
    excess_median: float | None
    beta_ridge: float | None
    beta_se: float | None
    conserved_amount: float | None
    conserved_share: float | None
    share_vs_baseline: float | None
    unassigned_amount: float
    up_share: float | None
    limit_up_count: int | None
    imbalance: float | None
    flags: tuple[str, ...] = ()


def _membership_parts(memberships: Any) -> tuple[dict[str, set[str]], dict[str, dict[str, Any]]]:
    raw = memberships.by_symbol if hasattr(memberships, "by_symbol") else memberships
    by_symbol: dict[str, set[str]] = {}
    meta: dict[str, dict[str, Any]] = {}
    for symbol, values in raw.items():
        names: set[str] = set()
        for item in values or ():
            if isinstance(item, str):
                plate, attrs = item, {}
            elif isinstance(item, Mapping):
                plate, attrs = item.get("plate"), item
            else:
                plate, attrs = getattr(item, "plate", None), {
                    key: getattr(item, key, None) for key in ("kind", "generic", "level", "parent")
                }
            if not plate:
                continue
            plate = str(plate)
            names.add(plate)
            current = meta.setdefault(plate, {})
            for key in ("kind", "generic", "level", "parent"):
                value = attrs.get(key)
                if value is not None:
                    current[key] = value
        by_symbol[str(symbol)] = names
    return by_symbol, meta


def _parents(hierarchy: Any) -> dict[str, str]:
    if hierarchy is None:
        return {}
    raw = hierarchy if isinstance(hierarchy, Mapping) else getattr(hierarchy, "parent_by_child", {})
    return {str(child): str(parent) for child, parent in raw.items() if child and parent}


def build_incidence(
    memberships: Any,
    hierarchy: Any,
    *,
    n_min: int = 10,
    include_kinds: Sequence[str] = ("THEME",),
    exclude_generic: bool = True,
    coverage_symbols: Sequence[str] | None = None,
) -> Incidence:
    """Build a boolean COO incidence matrix with ancestor closure and size pruning."""
    by_symbol, metadata = _membership_parts(memberships)
    parent = _parents(hierarchy)
    symbols = tuple(sorted(set(map(str, coverage_symbols)) if coverage_symbols is not None else by_symbol))
    closure: dict[str, set[str]] = {}
    for symbol in symbols:
        names = set(by_symbol.get(symbol, ()))
        for plate in tuple(names):
            seen = {plate}
            ancestor = parent.get(plate)
            while ancestor and ancestor not in seen:
                names.add(ancestor)
                seen.add(ancestor)
                ancestor = parent.get(ancestor)
        closure[symbol] = names
    kinds = set(include_kinds)
    all_names = set().union(*closure.values()) if closure else set()
    eligible = {
        name for name in all_names
        if metadata.get(name, {}).get("kind", "THEME") in kinds
        and not (exclude_generic and bool(metadata.get(name, {}).get("generic", False)))
    }
    sizes = {name: sum(name in names for names in closure.values()) for name in eligible}
    dropped: set[str] = set()
    retained = set(eligible)
    for name in tuple(sorted(eligible)):
        if sizes[name] >= n_min:
            continue
        ancestor = parent.get(name)
        while ancestor and ancestor not in eligible:
            ancestor = parent.get(ancestor)
        retained.discard(name)
        if ancestor is None:
            dropped.add(name)
    plates = tuple(sorted(retained))
    plate_index = {name: index for index, name in enumerate(plates)}
    symbol_index = {name: index for index, name in enumerate(symbols)}
    row_values: list[int] = []
    col_values: list[int] = []
    for symbol, names in closure.items():
        for name in sorted(names.intersection(retained)):
            row_values.append(symbol_index[symbol])
            col_values.append(plate_index[name])
    rows = np.asarray(row_values, dtype=np.int64)
    cols = np.asarray(col_values, dtype=np.int64)
    plate_meta: dict[str, dict[str, Any]] = {}
    for name in plates:
        ancestor = parent.get(name)
        level = metadata.get(name, {}).get("level")
        if level is None:
            depth, current, seen = 0, name, {name}
            while parent.get(current) and parent[current] not in seen:
                current = parent[current]
                seen.add(current)
                depth += 1
            level = depth
        plate_meta[name] = {
            "size": sizes[name], "kind": metadata.get(name, {}).get("kind", "THEME"),
            "level": level, "parent": ancestor,
        }
    return Incidence(symbols, plates, rows, cols, plate_meta, tuple(sorted(dropped)))


def _vector(incidence: Incidence, vectors: Mapping[str, Any], name: str) -> np.ndarray:
    raw = vectors.get(name)
    if isinstance(raw, Mapping):
        values = [raw.get(symbol, np.nan) for symbol in incidence.symbols]
    elif raw is None:
        values = [np.nan] * len(incidence.symbols)
    else:
        values = raw
    arr = np.asarray(values, dtype=float)
    if arr.shape != (len(incidence.symbols),):
        raise ValueError(f"vector {name!r} must have one value per incidence symbol")
    return arr


def _cg_beta(incidence: Incidence, y: np.ndarray, amount: np.ndarray,
             valid: np.ndarray, lam: float, amount_weighted: bool) -> tuple[np.ndarray, np.ndarray]:
    p_count, s_count = len(incidence.plates), len(incidence.symbols)
    if p_count == 0:
        return np.empty(0), np.empty(0)
    rows, cols = incidence.rows, incidence.cols
    obs = valid[rows]
    r, c = rows[obs], cols[obs]
    if r.size == 0:
        return np.zeros(p_count), np.full(p_count, np.nan)
    stock_wt = np.where(np.isfinite(amount[valid]) & (amount[valid] > 0), amount[valid], 1.0)
    mu = float(np.average(y[valid], weights=stock_wt)) if amount_weighted else float(np.mean(y[valid]))
    wt = (np.where(np.isfinite(amount[r]) & (amount[r] > 0), amount[r], 1.0)
          if amount_weighted else np.ones(r.size))
    centered = y[r] - mu
    rhs = np.bincount(c, weights=wt * centered, minlength=p_count)
    diagonal = np.bincount(c, weights=wt, minlength=p_count) + lam

    def matvec(value: np.ndarray) -> np.ndarray:
        fitted = np.bincount(r, weights=value[c], minlength=s_count)
        projected = np.bincount(c, weights=wt * fitted[r], minlength=p_count)
        return projected + lam * value

    beta = np.zeros(p_count)
    residual = rhs.copy()
    direction = residual.copy()
    rr = float(np.dot(residual, residual))
    for _ in range(min(max(20, p_count * 2), 500)):
        if rr < 1e-18:
            break
        product = matvec(direction)
        denom = float(np.dot(direction, product))
        if denom <= 0:
            break
        alpha = rr / denom
        beta += alpha * direction
        residual -= alpha * product
        next_rr = float(np.dot(residual, residual))
        direction = residual + (next_rr / rr) * direction
        rr = next_rr
    # Diagonal inverse approximation; bounded and conservative for correlated columns.
    se = np.sqrt(1.0 / np.maximum(diagonal, 1e-12))
    return beta, se


def compute_strength(
    incidence: Incidence,
    vectors: Mapping[str, Any],
    *,
    lam: float = 5.0,
    baseline_share: Mapping[str, float] | None = None,
    amount_weighted: bool = True,
) -> list[PlateStrength]:
    if lam < 0:
        raise ValueError("lam must be non-negative")
    y, amount = _vector(incidence, vectors, "y"), _vector(incidence, vectors, "amount")
    prev_close_ok = _vector(incidence, vectors, "prev_close_ok")
    rest_bid = _vector(incidence, vectors, "rest_bid")
    rest_ask = _vector(incidence, vectors, "rest_ask")
    limit_up = _vector(incidence, vectors, "limit_up")
    valid_y = np.isfinite(y) & (prev_close_ok > 0)
    stock_weights = np.where(np.isfinite(amount[valid_y]) & (amount[valid_y] > 0), amount[valid_y], 1.0)
    global_mean = (float(np.average(y[valid_y], weights=stock_weights)) if amount_weighted
                   else float(np.mean(y[valid_y]))) if np.any(valid_y) else np.nan
    beta, beta_se = _cg_beta(incidence, y, amount, valid_y, lam, amount_weighted)
    p_count = len(incidence.plates)
    membership_count = np.bincount(incidence.cols, minlength=p_count).astype(float)
    valid_count = np.bincount(incidence.cols[valid_y[incidence.rows]], minlength=p_count).astype(float)
    valid_amount_edges = np.isfinite(amount[incidence.rows]) & (amount[incidence.rows] >= 0)
    amt_cols = incidence.cols[valid_amount_edges]
    amt_rows = incidence.rows[valid_amount_edges]
    amt_sum = np.bincount(amt_cols, weights=amount[amt_rows], minlength=p_count)
    amount_y_edges = valid_amount_edges & valid_y[incidence.rows]
    ay_cols, ay_rows = incidence.cols[amount_y_edges], incidence.rows[amount_y_edges]
    ay_sum = np.bincount(ay_cols, weights=amount[ay_rows] * y[ay_rows], minlength=p_count)
    ay_amount = np.bincount(ay_cols, weights=amount[ay_rows], minlength=p_count)
    medians: dict[int, float] = {}
    med_indices = np.flatnonzero(valid_y[incidence.rows])
    med_order = med_indices[np.argsort(incidence.cols[med_indices], kind="stable")]
    if med_order.size:
        med_groups = incidence.cols[med_order]
        boundaries = np.r_[0, np.flatnonzero(med_groups[1:] != med_groups[:-1]) + 1, med_order.size]
        for left, right in zip(boundaries[:-1], boundaries[1:]):
            medians[int(med_groups[left])] = float(np.median(y[incidence.rows[med_order[left:right]]]))
    global_median = float(np.median(y[valid_y])) if np.any(valid_y) else np.nan
    # Conserved attribution uses only retained memberships and each stock's degree.
    degree = np.bincount(incidence.rows, minlength=len(incidence.symbols)).astype(float)
    edge_weight = np.divide(1.0, degree[incidence.rows], out=np.zeros(len(incidence.rows)),
                            where=degree[incidence.rows] > 0)
    conserved = np.bincount(incidence.cols[valid_amount_edges],
                            weights=amount[amt_rows] * edge_weight[valid_amount_edges],
                            minlength=p_count)
    assigned_rows = degree > 0
    total_allocated = float(np.sum(amount[assigned_rows & np.isfinite(amount) & (amount >= 0)]))
    conserved_share = np.divide(conserved, total_allocated, out=np.full(p_count, np.nan),
                                 where=total_allocated > 0)
    # Unmatched imbalance is apportioned by the same conserved membership weights.
    book_valid = (np.isfinite(rest_bid[incidence.rows]) & np.isfinite(rest_ask[incidence.rows])
                  & valid_amount_edges)
    bid = np.bincount(incidence.cols[book_valid],
                      weights=rest_bid[incidence.rows[book_valid]] * edge_weight[book_valid], minlength=p_count)
    ask = np.bincount(incidence.cols[book_valid],
                      weights=rest_ask[incidence.rows[book_valid]] * edge_weight[book_valid], minlength=p_count)
    match_valid = book_valid
    match = np.bincount(incidence.cols[match_valid],
                        weights=amount[incidence.rows[match_valid]] * edge_weight[match_valid], minlength=p_count)
    imbalance = np.divide(bid - ask, match, out=np.full(p_count, np.nan), where=match > 0)
    unassigned_amount = float(np.sum(amount[(degree == 0) & np.isfinite(amount) & (amount >= 0)]))
    limit_valid_edges = np.isfinite(limit_up[incidence.rows])
    limit_counts = np.bincount(incidence.cols[limit_valid_edges],
                               weights=(limit_up[incidence.rows[limit_valid_edges]] > 0).astype(float),
                               minlength=p_count)
    up_counts = np.bincount(incidence.cols[valid_y[incidence.rows]],
                            weights=(y[incidence.rows[valid_y[incidence.rows]]] > 0).astype(float),
                            minlength=p_count)
    result: list[PlateStrength] = []
    for i, plate in enumerate(incidence.plates):
        cov = valid_count[i] / membership_count[i] if membership_count[i] else 0.0
        eff = float(valid_count[i])
        flags = tuple(flag for flag, cond in (("UNKNOWN", cov < 0.6 or eff < 5),) if cond)
        excess_w = (ay_sum[i] / ay_amount[i] - global_mean
                    if ay_amount[i] > 0 and np.isfinite(global_mean)
                    else None)
        excess_med = medians[i] - global_median if i in medians and np.isfinite(global_median) else None
        base = baseline_share.get(plate) if baseline_share is not None else None
        ratio = conserved_share[i] / base if base is not None and base > 0 and np.isfinite(conserved_share[i]) else None
        result.append(PlateStrength(
            plate, int(membership_count[i]), eff, float(cov),
            float(excess_w) if excess_w is not None else None,
            float(excess_med) if excess_med is not None else None,
            float(beta[i]) if np.isfinite(beta[i]) else None,
            float(beta_se[i]) if np.isfinite(beta_se[i]) else None,
            float(conserved[i]) if np.isfinite(conserved[i]) else None,
            float(conserved_share[i]) if np.isfinite(conserved_share[i]) else None,
            float(ratio) if ratio is not None else None,
            unassigned_amount,
            float(up_counts[i] / valid_count[i]) if valid_count[i] else None,
            int(limit_counts[i]) if np.any(limit_valid_edges) else None,
            float(imbalance[i]) if np.isfinite(imbalance[i]) else None,
            flags,
        ))
    return result


def _rank_by_group(groups: np.ndarray, values: np.ndarray, valid: np.ndarray) -> np.ndarray:
    ranks = np.full(values.size, np.nan)
    indices = np.flatnonzero(valid)
    if not indices.size:
        return ranks
    order = indices[np.lexsort((values[indices], groups[indices]))]
    sorted_groups, sorted_values = groups[order], values[order]
    start = np.r_[0, np.flatnonzero((sorted_groups[1:] != sorted_groups[:-1]) |
                                    (sorted_values[1:] != sorted_values[:-1])) + 1]
    end = np.r_[start[1:], order.size]
    # Average rank for ties, expressed as percentile in [0, 1].
    tie_rank = (start + end - 1) / 2.0
    group_starts = np.r_[0, np.flatnonzero(sorted_groups[1:] != sorted_groups[:-1]) + 1]
    group_ends = np.r_[group_starts[1:], order.size]
    tie_to_group = np.searchsorted(group_starts, start, side="right") - 1
    denom = np.maximum(1, group_ends[tie_to_group] - group_starts[tie_to_group] - 1)
    pct = np.divide(tie_rank - group_starts[tie_to_group], denom,
                    out=np.full(tie_rank.size, 0.5), where=denom > 0)
    ranks[order] = pct[np.repeat(np.arange(start.size), end - start)]
    return ranks


def stock_relative(incidence: Incidence, vectors: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Return member return relative to leave-one-out plate mean and within-plate ranks."""
    y, amount = _vector(incidence, vectors, "y"), _vector(incidence, vectors, "amount")
    prev_close_ok = _vector(incidence, vectors, "prev_close_ok")
    y[~(prev_close_ok > 0)] = np.nan
    rows, cols = incidence.rows, incidence.cols
    valid = np.isfinite(y[rows])
    ys = y[rows]
    sums = np.bincount(cols[valid], weights=ys[valid], minlength=len(incidence.plates))
    counts = np.bincount(cols[valid], minlength=len(incidence.plates))
    others = counts[cols] - valid.astype(int)
    loo = np.divide(sums[cols] - np.where(valid, ys, 0.0), others,
                    out=np.full(rows.size, np.nan), where=others > 0)
    rel = ys - loo
    y_rank = _rank_by_group(cols, y[rows], valid)
    amt_valid = np.isfinite(amount[rows])
    amt_rank = _rank_by_group(cols, amount[rows], amt_valid)
    out: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in incidence.symbols}
    for edge, (row, col) in enumerate(zip(rows, cols)):
        out[incidence.symbols[row]].append({
            "plate": incidence.plates[col],
            "rel_leave_one_out": float(rel[edge]) if np.isfinite(rel[edge]) else None,
            "pct_rank_in_plate": float(y_rank[edge]) if np.isfinite(y_rank[edge]) else None,
            "amount_rank_in_plate": float(amt_rank[edge]) if np.isfinite(amt_rank[edge]) else None,
        })
    return out
