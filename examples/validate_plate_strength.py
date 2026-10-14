#!/usr/bin/env python3
"""Read-only robustness checks for plate_strength JSON session artifacts."""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import time
from pathlib import Path
from typing import Any, Mapping

from engine_core.plate_membership import PlateHierarchy
from engine_core.plate_strength import build_incidence, compute_strength

TIMES = ("0920", "0924", "0925", "0932")
LAM_VALUES = (2, 5, 10)
CAP_VALUES = (0.1, 0.15, 0.3)
N_MIN_VALUES = (8, 10, 15)


def _rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for key in ("stocks", "rows", "items", "data"):
            if isinstance(value.get(key), list):
                return [row for row in value[key] if isinstance(row, dict)]
    raise ValueError("expected a JSON list or an object containing a list of rows")


def _metadata(stocks: list[dict[str, Any]], plate_rows: list[dict[str, Any]]):
    meta = {}
    hierarchy = {}
    for row in plate_rows:
        name = row.get("plate", row.get("name"))
        if not name:
            continue
        meta[str(name)] = {key: row[key] for key in ("kind", "generic", "level") if key in row}
        parent = row.get("parent")
        if parent:
            hierarchy[str(name)] = str(parent)
    memberships = {}
    for stock in stocks:
        symbol = str(stock.get("symbol", stock.get("code", "")))
        items = stock.get("plates", stock.get("memberships", ())) or ()
        normalized = []
        for item in items:
            if isinstance(item, str):
                normalized.append(item)
                continue
            if not isinstance(item, dict):
                continue
            name = item.get("plate", item.get("name"))
            if not name:
                continue
            entry = dict(item)
            entry["plate"] = str(name)
            normalized.append(entry)
            meta.setdefault(str(name), {key: item[key] for key in ("kind", "generic", "level") if key in item})
            ancestors = item.get("ancestors") or ()
            if ancestors:
                hierarchy.setdefault(str(name), str(ancestors[0]))
            if item.get("parent"):
                hierarchy.setdefault(str(name), str(item["parent"]))
        if symbol:
            memberships[symbol] = normalized
    for name, attrs in meta.items():
        attrs.setdefault("kind", "THEME")
    return memberships, meta, hierarchy


def load_input(*, stocks_path=None, plates_path=None, memberships_path=None, facts_path=None):
    if bool(stocks_path) == bool(memberships_path):
        raise ValueError("choose exactly one input form: --stocks/--plates or --memberships/--facts")
    if stocks_path:
        stocks = _rows(json.loads(Path(stocks_path).read_text(encoding="utf-8")))
        plates = _rows(json.loads(Path(plates_path).read_text(encoding="utf-8"))) if plates_path else []
        memberships, meta, hierarchy = _metadata(stocks, plates)
        return stocks, memberships, meta, hierarchy

    memberships_json = json.loads(Path(memberships_path).read_text(encoding="utf-8"))
    facts = _rows(json.loads(Path(facts_path).read_text(encoding="utf-8")))
    raw = memberships_json.get("by_symbol", memberships_json) if isinstance(memberships_json, dict) else None
    if not isinstance(raw, dict):
        raise ValueError("memberships JSON must be a symbol-to-plates object")
    memberships = {str(symbol): (items or []) for symbol, items in raw.items()}
    stocks = facts
    meta = {}
    hierarchy = {}
    for values in memberships.values():
        for item in values:
            if isinstance(item, dict) and item.get("plate"):
                name = str(item["plate"])
                meta.setdefault(name, {key: item[key] for key in ("kind", "generic", "level") if key in item})
                parent = item.get("parent")
                if parent:
                    hierarchy.setdefault(name, str(parent))
                elif item.get("ancestors"):
                    hierarchy.setdefault(name, str(item["ancestors"][0]))
    for row in stocks:
        row.setdefault("plates", memberships.get(str(row.get("symbol", row.get("code", ""))), []))
    return stocks, memberships, meta, hierarchy


def _vectors(stocks, symbols, tag):
    by_symbol = {str(row.get("symbol", row.get("code", ""))): row for row in stocks}
    vectors = {key: {} for key in ("y", "amount", "prev_close_ok", "rest_bid", "rest_ask", "limit_up")}
    for symbol in symbols:
        row = by_symbol.get(symbol, {})
        point = (row.get("times") or {}).get(tag) or {}
        y = point.get("pct_vs_pre_close", point.get("pct", point.get("return")))
        price, prev = point.get("price"), row.get("pre_close")
        vectors["y"][symbol] = y
        vectors["amount"][symbol] = point.get("amount")
        vectors["prev_close_ok"][symbol] = bool(prev is not None and prev > 0 and y is not None)
        vectors["rest_bid"][symbol] = point.get("rest_bid")
        vectors["rest_ask"][symbol] = point.get("rest_ask")
        vectors["limit_up"][symbol] = 1 if point.get("limit_state") == 1 else 0
    return vectors


def _rankdata(mapping):
    ordered = sorted(mapping, key=lambda key: (mapping[key], key))
    ranks = {}
    i = 0
    while i < len(ordered):
        j = i + 1
        while j < len(ordered) and mapping[ordered[j]] == mapping[ordered[i]]:
            j += 1
        rank = (i + j - 1) / 2 + 1
        for key in ordered[i:j]:
            ranks[key] = rank
        i = j
    return ranks


def spearman(left: Mapping[str, float], right: Mapping[str, float]):
    keys = sorted(set(left).intersection(right))
    if len(keys) < 2:
        return None
    a, b = _rankdata({key: left[key] for key in keys}), _rankdata({key: right[key] for key in keys})
    mean_a, mean_b = statistics.mean(a.values()), statistics.mean(b.values())
    cov = sum((a[k] - mean_a) * (b[k] - mean_b) for k in keys)
    norm = math.sqrt(sum((a[k] - mean_a) ** 2 for k in keys) * sum((b[k] - mean_b) ** 2 for k in keys))
    return cov / norm if norm else 1.0


def _beta_map(rows):
    return {row.plate: row.beta_ridge for row in rows if row.beta_ridge is not None and math.isfinite(row.beta_ridge)}


def bootstrap_stability(stocks, plate_rows, hierarchy, tag, *, repeats=20, seed=41):
    memberships, _, _ = _metadata(stocks, plate_rows)
    symbols = sorted(memberships)
    parent_by_child = hierarchy if isinstance(hierarchy, Mapping) else {}
    inc = build_incidence(memberships, PlateHierarchy(parent_by_child, {}), n_min=10,
                          coverage_symbols=symbols)
    baseline = _beta_map(compute_strength(inc, _vectors(stocks, inc.symbols, tag)))
    rng = random.Random(seed)
    values = []
    for _ in range(repeats):
        keep = [symbol for symbol in symbols if rng.random() >= 0.1]
        sampled = {symbol: memberships[symbol] for symbol in keep}
        reduced = build_incidence(sampled, PlateHierarchy(parent_by_child, {}), n_min=10,
                                  coverage_symbols=keep)
        score = _beta_map(compute_strength(reduced, _vectors(stocks, reduced.symbols, tag)))
        rho = spearman(baseline, score)
        if rho is not None:
            values.append(rho)
    return {"repeats": repeats, "successful_repeats": len(values),
            "spearman_mean": statistics.mean(values) if values else None,
            "spearman_min": min(values) if values else None}


def conservation_error(allocated_amounts, distributed_amounts):
    expected, actual = sum(allocated_amounts), sum(distributed_amounts)
    return abs(actual - expected) / abs(expected) if expected else (0.0 if actual == 0 else math.inf)


def _conservation(stocks, inc, rows, vectors):
    import numpy as np

    amounts = np.asarray([vectors["amount"].get(symbol) for symbol in inc.symbols], dtype=float)
    degree = np.bincount(inc.rows, minlength=len(inc.symbols))
    assigned = (degree > 0) & np.isfinite(amounts) & (amounts >= 0)
    expected = float(amounts[assigned].sum())
    actual = sum(row.conserved_amount or 0.0 for row in rows)
    rel_error = abs(actual - expected) / abs(expected) if expected else (0.0 if actual == 0 else math.inf)
    return {"assigned_amount": expected, "distributed_amount": actual,
            "conservation_relative_error": rel_error}


def _parent_child_check(inc, hierarchy):
    edges = set(zip(map(int, inc.rows), map(int, inc.cols)))
    members = {plate: {inc.symbols[row] for row, col in edges if inc.plates[col] == plate}
               for plate in inc.plates}
    children = {}
    for child, parent in hierarchy.items():
        children.setdefault(parent, []).append(child)
    checked = 0
    violations = []
    equal = 0
    for parent, kids in children.items():
        if parent not in members:
            continue
        union = set().union(*(members.get(kid, set()) for kid in kids))
        checked += 1
        equal += members[parent] == union
        if not union.issubset(members[parent]):
            violations.append(parent)
    return {"duplicate_edges": len(inc.rows) - len(edges), "parent_child_pairs_checked": checked,
            "parents_equal_child_union": equal, "parent_missing_child_members": violations}


def assess_thresholds(*, bootstrap_mean, conservation, elapsed_s, top20_overlap_min):
    bootstrap_pass = bootstrap_mean is not None and bootstrap_mean >= 0.8
    conservation_pass = conservation <= 1e-9
    runtime_pass = elapsed_s < 1.0
    sensitivity_pass = top20_overlap_min is None or top20_overlap_min >= 14
    return {"pass": bootstrap_pass and conservation_pass and runtime_pass,
            "bootstrap_pass": bootstrap_pass, "conservation_pass": conservation_pass,
            "runtime_pass": runtime_pass, "sensitivity_advisory_pass": sensitivity_pass,
            "sensitivity_blocks": False}


def _sensitivity(memberships, hierarchy, stocks, tag, default):
    comparisons = []
    overlaps = []
    for lam in LAM_VALUES:
        for cap in CAP_VALUES:
            for n_min in N_MIN_VALUES:
                inc = build_incidence(memberships, PlateHierarchy(hierarchy, {}), n_min=n_min,
                                      coverage_symbols=sorted(memberships))
                changed = _beta_map(compute_strength(inc, _vectors(stocks, inc.symbols, tag),
                                                     lam=lam, weight_cap_share=cap))
                rho = spearman(default, changed)
                base_top = set(sorted(default, key=lambda name: (-default[name], name))[:20])
                changed_top = set(sorted(changed, key=lambda name: (-changed[name], name))[:20])
                overlap = len(base_top & changed_top)
                overlaps.append(overlap)
                comparisons.append({"lam": lam, "weight_cap_share": cap, "n_min": n_min,
                                    "rank_spearman": rho, "top20_intersection": overlap})
    return {"comparisons": comparisons, "top20_intersection_min": min(overlaps) if overlaps else None}


def run_validation(stocks, plate_rows=None, hierarchy=None, *, output_dir=None, sensitivity=True):
    hierarchy = dict(hierarchy or {})
    memberships, meta, inferred = _metadata(stocks, plate_rows or [])
    hierarchy = {**inferred, **hierarchy}
    if output_dir is not None:
        out = Path(output_dir)
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            raise FileExistsError(f"output directory must not exist or must be empty: {out}")
        out.mkdir(parents=True, exist_ok=True)
    full_started = time.perf_counter()
    time_results = {}
    total_build_wall = total_compute_wall = total_build_cpu = total_compute_cpu = 0.0
    for tag in TIMES:
        wall, cpu = time.perf_counter(), time.process_time()
        inc = build_incidence(memberships, PlateHierarchy(hierarchy, {}), n_min=10,
                              coverage_symbols=sorted(memberships))
        build_wall, build_cpu = time.perf_counter() - wall, time.process_time() - cpu
        vectors = _vectors(stocks, inc.symbols, tag)
        wall, cpu = time.perf_counter(), time.process_time()
        result = compute_strength(inc, vectors)
        compute_wall, compute_cpu = time.perf_counter() - wall, time.process_time() - cpu
        conservation = _conservation(stocks, inc, result, vectors)
        bootstrap = bootstrap_stability(stocks, plate_rows or [], hierarchy, tag)
        beta = _beta_map(result)
        sens = _sensitivity(memberships, hierarchy, stocks, tag, beta) if sensitivity else {
            "comparisons": [], "top20_intersection_min": None}
        time_results[tag] = {"plate_count": len(inc.plates), "rankable_plate_count": len(beta),
                             "bootstrap": bootstrap, "sensitivity": sens,
                             "conservation": conservation,
                             "incidence_integrity": _parent_child_check(inc, hierarchy),
                             "timing": {"build_incidence_wall_s": build_wall,
                                        "build_incidence_cpu_s": build_cpu,
                                        "compute_strength_wall_s": compute_wall,
                                        "compute_strength_cpu_s": compute_cpu}}
        total_build_wall += build_wall
        total_compute_wall += compute_wall
        total_build_cpu += build_cpu
        total_compute_cpu += compute_cpu
    baseline_elapsed = total_build_wall + total_compute_wall
    total_elapsed = time.perf_counter() - full_started
    max_conservation = max((row["conservation"]["conservation_relative_error"]
                            for row in time_results.values()), default=0.0)
    bootstrap_mean = statistics.mean([row["bootstrap"]["spearman_mean"] for row in time_results.values()
                                      if row["bootstrap"]["spearman_mean"] is not None]) if time_results else None
    top_min = min((row["sensitivity"]["top20_intersection_min"] for row in time_results.values()
                   if row["sensitivity"]["top20_intersection_min"] is not None), default=None)
    checks = assess_thresholds(bootstrap_mean=bootstrap_mean, conservation=max_conservation,
                               elapsed_s=baseline_elapsed, top20_overlap_min=top_min)
    result = {"times": time_results,
              "summary": {"baseline_four_time_wall_s": baseline_elapsed,
                          "analysis_total_wall_s": total_elapsed,
                          "build_incidence_wall_s": total_build_wall,
                          "build_incidence_cpu_s": total_build_cpu,
                          "compute_strength_wall_s": total_compute_wall,
                          "compute_strength_cpu_s": total_compute_cpu,
                          "bootstrap_spearman_mean": bootstrap_mean,
                          "conservation_relative_error_max": max_conservation,
                          "sensitivity_top20_intersection_min": top_min,
                          **checks}}
    if output_dir is not None:
        (Path(output_dir) / "validation.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        (Path(output_dir) / "README.md").write_text(_readme(result), encoding="utf-8")
    return result


def _readme(result):
    summary = result["summary"]
    return ("# Plate strength robustness validation\n\n"
            "Inputs are read-only JSON artifacts. Returns use each stock's `pct_vs_pre_close`; amount and "
            "order-book fields are passed through in their source units. `beta_ridge` is recomputed with "
            "the engine defaults at 09:20, 09:24, 09:25, and 09:32.\n\n"
            "## Checks\n\n"
            "The blocking pass requires mean bootstrap rank Spearman >= 0.8, conservation relative error "
            "<= 1e-9, and the four-time baseline calculation under 1 second. Sensitivity Top20 overlap "
            ">= 14 is advisory and does not block. Parent-child set relationships and duplicate edges "
            "are diagnostics. Bootstrap samples remove 10% of symbols for 20 repetitions with a fixed seed.\n\n"
            "## Results\n\n"
            f"- Overall pass: `{summary['pass']}`\n"
            f"- Baseline four-time wall: `{summary['baseline_four_time_wall_s']:.6f}s`\n"
            f"- Analysis total wall: `{summary['analysis_total_wall_s']:.6f}s`\n"
            f"- Mean bootstrap Spearman: `{summary['bootstrap_spearman_mean']}`\n"
            f"- Maximum conservation error: `{summary['conservation_relative_error_max']}`\n"
            f"- Minimum sensitivity Top20 intersection: `{summary['sensitivity_top20_intersection_min']}`\n\n"
            "## Risks\n\n"
            "This is a stability and accounting check, not evidence of predictive value. The parent-child "
            "diagnostic checks that every child member is included by its parent; a parent may also have "
            "direct members. Missing values remain missing in engine calculations.\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stocks")
    group.add_argument("--memberships")
    parser.add_argument("--plates")
    parser.add_argument("--facts")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    if args.stocks and not args.plates:
        parser.error("--stocks requires --plates")
    if args.memberships and not args.facts:
        parser.error("--memberships requires --facts")
    stocks, memberships, meta, hierarchy = load_input(
        stocks_path=args.stocks, plates_path=args.plates,
        memberships_path=args.memberships, facts_path=args.facts)
    result = run_validation(stocks, list(meta.values()), hierarchy, output_dir=args.output_dir)
    print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    return 0 if result["summary"]["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
