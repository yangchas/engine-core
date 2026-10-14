import math

from engine_core.plate_significance import (
    _bh,
    detect_significant_plates,
    select_independent_plates,
)


def test_detects_real_strong_plate_and_not_random_plate():
    state = 12345
    returns = {}
    for index in range(1200):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        returns[f"S{index:04d}"] = ((state % 2001) - 1000) / 10000
    strong = {f"S{i:04d}" for i in range(20)}
    for symbol in strong:
        returns[symbol] += 0.08
    random_plate = {f"S{i:04d}" for i in range(200, 230)}
    members = {"strong": strong, "random": random_plate}
    registry = {code: {"name": code, "class": "THEME"} for code in members}

    result = detect_significant_plates(returns, members, registry)
    rows = {row["code"]: row for row in result.plates}
    assert rows["strong"]["status"] == "SIGNIFICANT_STRONG"
    assert rows["random"]["status"] == "NO_SIGNAL"


def test_bh_adjusted_values_match_hand_calculation_boundary():
    rows = [
        {"code": "a", "p": 0.01, "z": 2.0},
        {"code": "b", "p": 0.04, "z": 1.0},
        {"code": "c", "p": 0.20, "z": -0.1},
    ]
    _bh(rows, 0.05)
    by_code = {row["code"]: row for row in rows}
    assert by_code["a"]["q"] == 0.03
    assert by_code["b"]["q"] == 0.06
    assert math.isclose(by_code["c"]["q"], 0.20)
    assert [by_code[code]["status"] for code in ("a", "b", "c")] == [
        "SIGNIFICANT_STRONG", "NO_SIGNAL", "NO_SIGNAL"
    ]


def test_missing_returns_are_excluded_not_zero_filled():
    returns = {"a": 0.10, "b": 0.11, "c": None, "d": 0.09, "e": 0.10}
    members = {"p": {"a", "b", "c", "d", "e", "missing"}}
    registry = {"p": {"class": "THEME"}}
    result = detect_significant_plates(returns, members, registry, min_n=4)
    row = result.plates[0]
    assert result.missing_count == 2
    assert row["n"] == 4
    assert math.isclose(row["trim_mean"], 0.10)


def test_weight_cap_reduces_single_limit_up_symbol_dominance():
    returns = {"hot": 0.30}
    returns.update({f"x{i}": (i - 9) / 100 for i in range(19)})
    members = {"p": set(returns)}
    registry = {"p": {"class": "THEME"}}
    weights = {"hot": 100.0, **{f"x{i}": 1.0 for i in range(19)}}
    uncapped = detect_significant_plates(
        returns, members, registry, weights=weights, weight_cap_share=1.0,
        limit_up_symbols={"hot"},
    ).plates[0]
    capped = detect_significant_plates(
        returns, members, registry, weights=weights, weight_cap_share=0.25,
        limit_up_symbols={"hot"},
    ).plates[0]
    assert capped["z"] < uncapped["z"]
    assert capped["limit_up_count"] == 1
    assert capped["top1_weight_share"] <= 0.25 + 1e-12


def test_parent_is_retested_on_residual_after_significant_child():
    returns = {f"c{i}": 0.10 + i * 0.0001 for i in range(20)}
    returns.update({f"r{i}": 0.0 + i * 0.0001 for i in range(20)})
    returns.update({f"m{i}": ((i % 7) - 3) * 0.0001 for i in range(160)})
    child = {f"c{i}" for i in range(20)}
    parent = child | {f"r{i}" for i in range(20)}
    members = {"parent": parent, "child": child}
    registry = {
        "parent": {"class": "BROAD", "children": ["child"]},
        "child": {"class": "THEME", "parent": "parent"},
    }
    result = detect_significant_plates(returns, members, registry)
    rows = {row["code"]: row for row in result.plates}
    assert rows["child"]["status"] == "SIGNIFICANT_STRONG"
    assert rows["parent"]["significant_children"] == 1
    assert rows["parent"]["residual_n"] == 20
    assert math.isfinite(rows["parent"]["residual_z"])


def test_generic_and_event_are_controls_not_listed_plates():
    returns = {f"s{i}": i / 1000 for i in range(30)}
    members = {"generic": set(returns), "event": set(returns)}
    registry = {
        "generic": {"class": "GENERIC_FACTOR"},
        "event": {"class": "EVENT"},
    }
    result = detect_significant_plates(returns, members, registry)
    assert result.plates == []
    assert set(result.controls) == {"generic", "event"}
    assert all(math.isfinite(value) for value in result.controls.values())


def test_independent_selection_folds_highly_overlapping_strong_plates():
    returns = {f"s{i}": ((i % 7) - 3) / 1000 for i in range(200)}
    shared = {f"s{i}" for i in range(30)}
    for symbol in shared:
        returns[symbol] += 0.08
    members = {"a": shared, "b": set(shared)}
    registry = {code: {"name": code, "class": "THEME"} for code in members}

    result = select_independent_plates(returns, members, registry)

    assert [row["code"] for row in result.plates] == ["a"]
    redundant = {row["code"]: row for row in result.candidates}["b"]
    assert redundant["status"] == "REDUNDANT"
    assert redundant["covered_by"] == "a"


def test_independent_selection_keeps_disjoint_strong_plates():
    returns = {f"s{i}": ((i % 7) - 3) / 1000 for i in range(300)}
    first = {f"s{i}" for i in range(20)}
    second = {f"s{i}" for i in range(100, 120)}
    for symbol in first | second:
        returns[symbol] += 0.08
    members = {"a": first, "b": second}
    registry = {code: {"class": "THEME"} for code in members}

    result = select_independent_plates(returns, members, registry)

    assert {row["code"] for row in result.plates} == {"a", "b"}
    assert all(row["direction"] == "STRONG" for row in result.plates)


def test_independent_selection_retests_weak_plate_on_residual_members():
    returns = {f"s{i}": ((i % 9) - 4) / 1000 for i in range(300)}
    dominant = {f"s{i}" for i in range(30)}
    residual = {f"s{i}" for i in range(30, 60)}
    for symbol in dominant:
        returns[symbol] += 0.12
    members = {"dominant": dominant, "mixed": dominant | residual}
    registry = {code: {"class": "THEME"} for code in members}

    result = select_independent_plates(returns, members, registry)

    assert [row["code"] for row in result.plates] == ["dominant"]
    mixed = {row["code"]: row for row in result.candidates}["mixed"]
    assert mixed["residual_n"] == len(residual)
    assert mixed["status"] == "NO_SIGNAL"


def test_independent_selection_returns_no_plates_for_noise():
    returns = {f"s{i}": ((i * 37) % 101 - 50) / 10000 for i in range(500)}
    members = {f"p{i}": {f"s{j}" for j in range(i * 10, i * 10 + 30)} for i in range(20)}
    registry = {code: {"class": "THEME"} for code in members}
    members["generic"] = set(returns)
    registry["generic"] = {"class": "GENERIC_FACTOR"}

    result = select_independent_plates(returns, members, registry)

    assert result.plates == []
    assert result.rounds == 1
    assert "generic" in result.controls


def test_independent_selection_max_selected_and_uncovered_summary():
    returns = {f"s{i}": ((i % 7) - 3) / 1000 for i in range(120)}
    first = {f"s{i}" for i in range(20)}
    second = {f"s{i}" for i in range(40, 60)}
    for symbol in first | second:
        returns[symbol] += 0.08
    members = {"a": first, "b": second}
    registry = {code: {"class": "THEME"} for code in members}

    result = select_independent_plates(returns, members, registry, max_selected=1)

    assert len(result.plates) == 1
    assert result.rounds == 1
    assert result.uncovered["n"] == 100
    assert math.isfinite(result.uncovered["median"])
    assert math.isfinite(result.uncovered["trim_mean"])
    assert result.uncovered["p10"] <= result.uncovered["median"] <= result.uncovered["p90"]


def test_independent_selection_missing_returns_are_not_zero_filled():
    returns = {"a": 0.10, "b": 0.11, "c": None, "d": 0.09, "e": 0.10}
    members = {"p": set(returns) | {"missing"}}
    registry = {"p": {"class": "THEME"}}

    result = select_independent_plates(returns, members, registry, min_n=4)

    assert result.missing_count == 2
    assert result.candidates[0]["n"] == 4
    assert result.uncovered["n"] == 4
