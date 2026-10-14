import numpy as np
from time import process_time as perf_counter

from engine_core.plate_membership import PlateHierarchy
from engine_core.plate_strength import build_incidence, compute_strength, stock_relative


def test_incidence_closes_parent_membership_and_deduplicates_ancestor_paths():
    memberships = {"s1": ["child", "parent"], "s2": ["child"], "s3": ["parent"]}
    hierarchy = PlateHierarchy({"child": "parent"}, {})
    incidence = build_incidence(memberships, hierarchy, n_min=1,
                                coverage_symbols=("s1", "s2", "s3"))

    child_members = {incidence.symbols[r] for r, c in zip(incidence.rows, incidence.cols)
                     if incidence.plates[c] == "child"}
    parent_members = {incidence.symbols[r] for r, c in zip(incidence.rows, incidence.cols)
                      if incidence.plates[c] == "parent"}
    assert child_members == {"s1", "s2"}
    assert parent_members == {"s1", "s2", "s3"}


def test_conserved_amount_sums_to_assigned_market_amount():
    symbols = [f"s{i}" for i in range(7)]
    memberships = {s: (["a", "b"] if i < 3 else ["b"]) for i, s in enumerate(symbols)}
    memberships["s6"] = []
    inc = build_incidence(memberships, PlateHierarchy({}, {}), n_min=1, coverage_symbols=symbols)
    rows = compute_strength(inc, {"y": [1, 2, 3, 4, 5, 6, 7], "amount": [10, 20, 30, 40, 50, 60, 70],
                                 "prev_close_ok": [True] * 7}, lam=1)
    assert sum(row.conserved_amount for row in rows) == 210
    assert rows[0].unassigned_amount == 70


def test_ridge_recovers_overlapping_plate_effects():
    symbols = [f"s{i}" for i in range(60)]
    memberships = {s: (["a", "b"] if i % 3 == 0 else ["a"] if i % 3 == 1 else ["b"])
                   for i, s in enumerate(symbols)}
    inc = build_incidence(memberships, PlateHierarchy({}, {}), n_min=1, coverage_symbols=symbols)
    y = {s: 2.0 * ("a" in memberships[s]) + 3.0 * ("b" in memberships[s]) for s in symbols}
    rows = compute_strength(inc, {"y": y, "amount": np.ones(len(symbols)),
                                  "prev_close_ok": {s: True for s in symbols}}, lam=0.01)
    beta = {row.plate: row.beta_ridge for row in rows}
    assert np.corrcoef([2, 3], [beta["a"], beta["b"]])[0, 1] > 0.99


def test_missing_values_remain_missing_and_low_coverage_is_unknown():
    syms = [f"s{i}" for i in range(10)]
    inc = build_incidence({s: ["a"] for s in syms}, PlateHierarchy({}, {}), n_min=1,
                          coverage_symbols=syms)
    row = compute_strength(inc, {"y": [1.0, np.nan] + [np.nan] * 8,
                                 "amount": [10.0] + [np.nan] * 9,
                                 "prev_close_ok": [True] * 10})[0]
    assert row.n_eff == 1
    assert row.coverage == 0.1
    assert "UNKNOWN" in row.flags
    assert row.conserved_amount == 10


def test_ridge_shrinks_small_plate_toward_zero():
    inc = build_incidence({"s0": ["tiny"], "s1": ["tiny"], "s2": ["other"], "s3": ["other"]},
                          PlateHierarchy({}, {}), n_min=1, coverage_symbols=("s0", "s1", "s2", "s3"))
    vectors = {"y": {"s0": 8.0, "s1": 8.0, "s2": 2.0, "s3": 2.0},
               "prev_close_ok": {s: True for s in ("s0", "s1", "s2", "s3")},
               "amount": {s: 1.0 for s in ("s0", "s1", "s2", "s3")}}
    shrunk = {r.plate: r.beta_ridge for r in compute_strength(inc, vectors, lam=5)}["tiny"]
    unshrunk = {r.plate: r.beta_ridge for r in compute_strength(inc, vectors, lam=0)}["tiny"]
    assert abs(shrunk) < abs(unshrunk)


def test_leave_one_out_and_ranks_are_vectorized_facts():
    inc = build_incidence({"a": ["p"], "b": ["p"], "c": ["p"]},
                          PlateHierarchy({}, {}), n_min=1, coverage_symbols=("a", "b", "c"))
    facts = stock_relative(inc, {"y": {"a": 1, "b": 2, "c": 3},
                                  "prev_close_ok": {s: True for s in ("a", "b", "c")},
                                  "amount": {"a": 10, "b": 20, "c": 30}})
    assert facts["a"][0]["rel_leave_one_out"] == -1.5
    assert facts["b"][0]["rel_leave_one_out"] == 0
    assert facts["c"][0]["rel_leave_one_out"] == 1.5
    assert facts["a"][0]["pct_rank_in_plate"] == 0
    assert facts["c"][0]["amount_rank_in_plate"] == 1


def test_random_member_removal_keeps_plate_strength_order_stable():
    rng = np.random.default_rng(41)
    symbols = [f"s{i}" for i in range(400)]
    memberships = {s: (["a", "b"] if i % 4 == 0 else ["a"] if i % 4 < 2 else ["b"])
                   for i, s in enumerate(symbols)}
    y = np.asarray([4 if "a" in memberships[s] else -2 for s in symbols], dtype=float)
    inc = build_incidence(memberships, PlateHierarchy({}, {}), n_min=1, coverage_symbols=symbols)
    baseline = [r.beta_ridge for r in compute_strength(inc, {"y": y, "amount": np.ones(len(y)),
                                                              "prev_close_ok": np.ones(len(y))}, lam=0.1)]
    keep = rng.random(len(symbols)) >= 0.1
    subset = [s for s, retain in zip(symbols, keep) if retain]
    reduced = build_incidence({s: memberships[s] for s in subset}, PlateHierarchy({}, {}), n_min=1,
                              coverage_symbols=subset)
    changed = [r.beta_ridge for r in compute_strength(
        reduced, {"y": {s: y[int(s[1:])] for s in subset}, "amount": {s: 1 for s in subset},
                  "prev_close_ok": {s: True for s in subset}}, lam=0.1)]
    assert np.corrcoef(baseline, changed)[0, 1] > 0.8


def test_5k_symbol_400_plate_20k_edge_budget():
    symbols = [f"s{i}" for i in range(5000)]
    memberships = {s: [f"p{(i + j * 137) % 400}" for j in range(4)]
                   for i, s in enumerate(symbols)}
    started = perf_counter()
    inc = build_incidence(memberships, PlateHierarchy({}, {}), n_min=1, coverage_symbols=symbols)
    compute_strength(inc, {"y": {s: float(i % 17) for i, s in enumerate(symbols)},
                           "amount": {s: float(i + 1) for i, s in enumerate(symbols)},
                           "prev_close_ok": {s: True for s in symbols}})
    stock_relative(inc, {"y": {s: float(i % 17) for i, s in enumerate(symbols)},
                         "amount": {s: float(i + 1) for i, s in enumerate(symbols)},
                         "prev_close_ok": {s: True for s in symbols}})
    assert len(inc.rows) == 20000
    assert perf_counter() - started < 1.0


def test_strength_caps_single_stock_and_reports_concentration():
    symbols = ["leader"] + [f"s{i}" for i in range(9)] + [f"t{i}" for i in range(10)]
    memberships = {s: ["p"] for s in symbols[:10]}
    memberships.update({s: ["control"] for s in symbols[10:]})
    inc = build_incidence(memberships, PlateHierarchy({}, {}),
                          n_min=1, coverage_symbols=symbols)
    vectors = {"y": {s: (10.0 if s == "leader" else 0.0) for s in symbols},
               "amount": {s: (800.0 if s == "leader" else 25.0) for s in symbols},
               "prev_close_ok": {s: True for s in symbols}}
    row = {r.plate: r for r in compute_strength(inc, vectors, lam=0, weight_mode="amount")}['p']
    uncapped = {r.plate: r for r in compute_strength(
        inc, vectors, lam=0, weight_mode="amount", weight_cap_share=1.0)}['p']
    assert abs(row.excess_amt_weighted) < abs(uncapped.excess_amt_weighted)
    assert row.top1_amount_share == 800 / 1025
    assert row.top3_amount_share > row.top1_amount_share
    assert row.hhi_amount > 0.5
    assert row.dominant_symbol == "leader"
    assert "SINGLE_STOCK_DOMINATED" in row.flags


def test_trimmed_and_winsorized_strength_resist_return_outlier():
    symbols = [f"s{i}" for i in range(40)]
    memberships = {s: (["p"] if i < 20 else ["control"]) for i, s in enumerate(symbols)}
    inc = build_incidence(memberships, PlateHierarchy({}, {}),
                          n_min=1, coverage_symbols=symbols)
    y = {s: (100.0 if s == "s19" else 0.0) for s in symbols}
    rows = {r.plate: r for r in compute_strength(
        inc, {"y": y, "amount": {s: 1.0 for s in symbols},
              "prev_close_ok": {s: True for s in symbols}}, lam=0)}
    assert rows["p"].excess_trimmed == 0.0
    assert rows["p"].beta_ridge_winsor < rows["p"].beta_ridge
    assert "OUTLIER_DRIVEN" in rows["p"].flags


def test_equal_weight_mode_and_beta_se_scale_with_plate_size():
    symbols = [f"s{i}" for i in range(12)]
    membership = {s: (["small"] if i < 2 else ["large"]) for i, s in enumerate(symbols)}
    inc = build_incidence(membership, PlateHierarchy({}, {}), n_min=1, coverage_symbols=symbols)
    y = [2.0, 4.0] + [2.0, 4.0] * 5
    amounts = [1000.0, 1.0] + [1.0, 1.0] * 5
    rows = {r.plate: r for r in compute_strength(
        inc, {"y": y, "amount": amounts, "prev_close_ok": [True] * 12},
        weight_mode="equal", lam=0.1)}
    assert rows["small"].excess_amt_weighted == 3.0 - np.mean(y)
    assert rows["small"].beta_se > 0
    assert rows["small"].beta_se > rows["large"].beta_se
