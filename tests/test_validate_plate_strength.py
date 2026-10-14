import json

import pytest

from examples.validate_plate_strength import (
    assess_thresholds,
    bootstrap_stability,
    conservation_error,
    run_validation,
)


def _fixture():
    stocks = []
    plates = []
    for i in range(40):
        memberships = ["parent", "child_a"] if i < 20 else ["parent", "child_b"]
        stocks.append({
            "symbol": f"s{i:03}",
            "pre_close": 10.0,
            "plates": [{"plate": name, "kind": "THEME", "generic": False}
                       for name in memberships],
            "times": {
                tag: {"pct_vs_pre_close": (0.04 if i < 20 else -0.02),
                      "amount": 100.0 + i, "rest_bid": 5.0, "rest_ask": 3.0,
                      "limit_state": 0}
                for tag in ("0920", "0924", "0925", "0932")
            },
        })
    for name in ("parent", "child_a", "child_b"):
        plates.append({"plate": name, "kind": "THEME", "generic": False})
    return stocks, plates, {"child_a": "parent", "child_b": "parent"}


def test_bootstrap_is_reproducible_and_reports_rank_stability():
    stocks, plates, hierarchy = _fixture()
    first = bootstrap_stability(stocks, plates, hierarchy, "0920", repeats=20, seed=17)
    second = bootstrap_stability(stocks, plates, hierarchy, "0920", repeats=20, seed=17)
    assert first == second
    assert first["repeats"] == 20
    assert first["spearman_mean"] == pytest.approx(1.0)


def test_conservation_error_is_zero_for_allocated_amounts():
    stocks, plates, hierarchy = _fixture()
    result = run_validation(stocks, plates, hierarchy, sensitivity=False)
    assert all(row["conservation"]["conservation_relative_error"] <= 1e-12
               for row in result["times"].values())


def test_nonempty_output_directory_is_rejected(tmp_path):
    stocks, plates, hierarchy = _fixture()
    (tmp_path / "existing").write_text("keep")
    with pytest.raises(FileExistsError):
        run_validation(stocks, plates, hierarchy, output_dir=tmp_path, sensitivity=False)


def test_thresholds_distinguish_blocking_and_advisory_failures():
    checks = assess_thresholds(bootstrap_mean=0.79, conservation=1e-10,
                               elapsed_s=0.9, top20_overlap_min=13)
    assert checks["pass"] is False
    assert checks["bootstrap_pass"] is False
    assert checks["conservation_pass"] is True
    assert checks["runtime_pass"] is True
    assert checks["sensitivity_advisory_pass"] is False
    assert conservation_error([10.0, 20.0], [10.0, 20.0]) == 0.0
