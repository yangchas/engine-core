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
    excess_trimmed: float | None = None
    beta_ridge_winsor: float | None = None
    top1_amount_share: float | None = None
    top3_amount_share: float | None = None
    hhi_amount: float | None = None
    dominant_symbol: str | None = None


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


def _cg_beta(incidence: Incidence, y: np.ndarray, valid: np.ndarray,
             edge_weights: np.ndarray, lam: float) -> tuple[np.ndarray, np.ndarray]:
    p_count, s_count = len(incidence.plates), len(incidence.symbols)
    if p_count == 0:
        return np.empty(0), np.empty(0)
    rows, cols = incidence.rows, incidence.cols
    obs = valid[rows]
    r, c = rows[obs], cols[obs]
    if r.size == 0:
        return np.zeros(p_count), np.full(p_count, np.nan)
    wt = edge_weights[obs]
    mu = float(np.average(y[r], weights=np.maximum(wt, 1e-12)))
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
    fitted = np.bincount(r, weights=beta[c], minlength=s_count)
    residuals = y[r] - mu - fitted[r]
    dof = max(1, int(r.size) - p_count)
    sigma2 = float(np.dot(wt, residuals * residuals) / dof)
    # Diagonal of (B'WB + lam I)^-1, using its diagonal approximation.
    se = np.sqrt(max(sigma2, 1e-24) / np.maximum(diagonal, 1e-12))
    return beta, se


def _base_weights(amount: np.ndarray, mode: str) -> np.ndarray:
    positive = np.isfinite(amount) & (amount > 0)
    values = np.where(positive, amount, 1.0)
    if mode == "equal":
        return np.ones(amount.size, dtype=float)
    if mode == "sqrt_amount":
        return np.sqrt(values)
    if mode == "amount":
        return values
    raise ValueError("weight_mode must be 'sqrt_amount', 'equal', or 'amount'")


def _capped_edge_weights(incidence: Incidence, amount: np.ndarray, valid: np.ndarray,
                         mode: str, cap_share: float) -> np.ndarray:
    weights = np.zeros(incidence.rows.size, dtype=float)
    base = _base_weights(amount, mode)
    for plate_index in range(len(incidence.plates)):
        edges = np.flatnonzero((incidence.cols == plate_index) & valid[incidence.rows])
        if not edges.size:
            continue
        raw = base[incidence.rows[edges]]
        raw = raw / np.sum(raw)
        cap = max(cap_share, 1.0 / edges.size)  # A lower cap cannot sum to one for small plates.
        current = raw.copy()
        for _ in range(5):
            over = current > cap + 1e-12
            if not np.any(over):
                break
            current[over] = cap
            free = ~over
            remaining = 1.0 - float(np.sum(current[over]))
            if not np.any(free) or remaining <= 0:
                break
            current[free] = raw[free] / np.sum(raw[free]) * remaining
        if np.any(current > cap + 1e-10):
            current.fill(1.0 / edges.size)
        # Keep per-plate relative shares capped while retaining sample-size scale for ridge.
        weights[edges] = current * edges.size
    return weights


def compute_strength(
    incidence: Incidence,
    vectors: Mapping[str, Any],
    *,
    lam: float = 5.0,
    baseline_share: Mapping[str, float] | None = None,
    amount_weighted: bool = True,
    weight_cap_share: float = 0.15,
    weight_mode: str = "sqrt_amount",
    divergence_pp: float = 2.0,
) -> list[PlateStrength]:
    if lam < 0:
        raise ValueError("lam must be non-negative")
    if not 0 < weight_cap_share <= 1:
        raise ValueError("weight_cap_share must be in (0, 1]")
    if divergence_pp < 0:
        raise ValueError("divergence_pp must be non-negative")
    if weight_mode not in {"sqrt_amount", "equal", "amount"}:
        raise ValueError("weight_mode must be 'sqrt_amount', 'equal', or 'amount'")
    y, amount = _vector(incidence, vectors, "y"), _vector(incidence, vectors, "amount")
    prev_close_ok = _vector(incidence, vectors, "prev_close_ok")
    rest_bid = _vector(incidence, vectors, "rest_bid")
    rest_ask = _vector(incidence, vectors, "rest_ask")
    limit_up = _vector(incidence, vectors, "limit_up")
    valid_y = np.isfinite(y) & (prev_close_ok > 0)
    effective_mode = weight_mode if amount_weighted else "equal"
    edge_weights = _capped_edge_weights(incidence, amount, valid_y, effective_mode, weight_cap_share)
    weighted_degree = np.bincount(incidence.rows, weights=edge_weights, minlength=len(incidence.symbols))
    edge_degree = np.bincount(incidence.rows, minlength=len(incidence.symbols))
    stock_weights = np.divide(weighted_degree, edge_degree, out=np.zeros_like(weighted_degree),
                               where=edge_degree > 0)
    unassigned_valid = valid_y & (edge_degree == 0)
    stock_weights[unassigned_valid] = _base_weights(amount[unassigned_valid], effective_mode)
    global_mean = (float(np.average(y[valid_y], weights=np.maximum(stock_weights[valid_y], 1e-12)))
                   if np.any(valid_y) else np.nan)
    beta, beta_se = _cg_beta(incidence, y, valid_y, edge_weights, lam)
    if np.any(valid_y):
        y_winsor = y.copy()
        p1, p99 = np.percentile(y[valid_y], [1, 99])
        y_winsor[valid_y] = np.clip(y[valid_y], p1, p99)
        beta_winsor, _ = _cg_beta(incidence, y_winsor, valid_y, edge_weights, lam)
    else:
        beta_winsor = np.full(len(incidence.plates), np.nan)
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
    trimmed: dict[int, float] = {}
    for plate_index in range(len(incidence.plates)):
        values = y[incidence.rows[(incidence.cols == plate_index) & valid_y[incidence.rows]]]
        if values.size:
            ordered = np.sort(values)
            trim = int(np.floor(ordered.size * 0.1))
            kept = ordered[trim:ordered.size - trim] if trim else ordered
            trimmed[plate_index] = float(np.mean(kept))
    amount_concentration: dict[int, tuple[float | None, float | None, float | None, str | None]] = {}
    for plate_index in range(p_count):
        edges = np.flatnonzero((incidence.cols == plate_index) & valid_amount_edges)
        if not edges.size:
            amount_concentration[plate_index] = (None, None, None, None)
            continue
        edge_amount = amount[incidence.rows[edges]]
        total = float(np.sum(edge_amount))
        if total <= 0:
            amount_concentration[plate_index] = (None, None, None, None)
            continue
        order = np.lexsort((np.asarray([incidence.symbols[r] for r in incidence.rows[edges]]), -edge_amount))
        shares = edge_amount[order] / total
        top_symbol = incidence.symbols[incidence.rows[edges[order[0]]]]
        amount_concentration[plate_index] = (
            float(shares[0]), float(np.sum(shares[:3])), float(np.dot(shares, shares)),
            top_symbol if shares[0] >= 0.5 else None,
        )
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
        top1, top3, hhi, dominant = amount_concentration[i]
        flags_list = [flag for flag, cond in (("UNKNOWN", cov < 0.6 or eff < 5),) if cond]
        if top1 is not None and top1 >= 0.5:
            flags_list.append("SINGLE_STOCK_DOMINATED")
        plate_edges = np.flatnonzero((incidence.cols == i) & valid_y[incidence.rows])
        plate_values = y[incidence.rows[plate_edges]]
        plate_weights = edge_weights[plate_edges]
        excess_w = (float(np.average(plate_values, weights=plate_weights)) - global_mean
                    if plate_edges.size and np.isfinite(global_mean)
                    else None)
        excess_med = medians[i] - global_median if i in medians and np.isfinite(global_median) else None
        excess_trim = trimmed[i] - global_median if i in trimmed and np.isfinite(global_median) else None
        beta_winsor_value = float(beta_winsor[i]) if np.isfinite(beta_winsor[i]) else None
        if ((beta_winsor_value is not None and np.isfinite(beta[i])
             and abs(float(beta[i]) - beta_winsor_value) > divergence_pp)
                or (excess_w is not None and excess_med is not None
                    and abs(excess_w - excess_med) > divergence_pp)):
            flags_list.append("OUTLIER_DRIVEN")
        flags = tuple(flags_list)
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
            flags, excess_trim, beta_winsor_value, top1, top3, hhi, dominant,
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
