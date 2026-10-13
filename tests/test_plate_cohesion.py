from engine_core.plate_cohesion import (
    active_plates_for_stock,
    align_asof_grid,
    calculate_plate_cohesion,
    lead_lag,
)
from engine_core.plate_membership import PlateHierarchy, build_memberships


def _members(*symbols):
    return build_memberships(
        symbols=symbols,
        pool_plate_by_symbol={symbol: "人工智能" for symbol in symbols},
        reason_candidates_by_symbol={}, static_themes_by_symbol={},
    )


def test_grid_uses_latest_asof_value_without_looking_ahead():
    rows = {"x": [(0, 10.0, 0.0), (2500, 11.0, 20.0)]}
    grid = align_asof_grid(rows, 0, 6000, grid_ms=3000)
    assert grid["x"] == [(0, 10.0, 0.0), (3000, 11.0, 20.0), (6000, 11.0, 20.0)]


def test_synchronous_rise_and_lagging_follower_are_facts():
    symbols = ("a", "b", "c", "d", "e", "f")
    memberships = _members(*symbols)
    hierarchy = PlateHierarchy({}, {})
    prices = {}
    for symbol in symbols:
        delay = 2 if symbol == "f" else 0
        values = [100.0]
        increments = [0.1, 0.2, 0.4, 0.3, 0.6, 0.2, 0.7, 0.5, 0.9, 0.4, 1.0, 0.6]
        for i in range(14):
            step = increments[i - delay] if delay <= i < delay + len(increments) else 0.0
            values.append(values[-1] + step)
        prices[symbol] = [(i * 3000, value, (i + 1) * 100.0) for i, value in enumerate(values)]
    result = calculate_plate_cohesion(
        prices, {symbol: 100.0 for symbol in symbols}, memberships, hierarchy,
        grid_start_ms=0, grid_end_ms=39000,
    )
    plate = next(row for row in result.cohesion if row.plate == "人工智能")
    assert plate.same_direction_share["up"] == 1.0
    affinity = next(row for row in result.affinities if row.symbol == "f" and row.plate == "人工智能")
    assert affinity.best_lag_points == 2
    assert affinity.role == "FOLLOWER"
    assert active_plates_for_stock([affinity])[0].plate == "人工智能"


def test_insufficient_members_and_points_are_flagged_and_metrics_unknown():
    memberships = _members("a", "b")
    result = calculate_plate_cohesion(
        {"a": [(0, 10, 1)], "b": [(0, 10, 2)]}, {"a": 10, "b": 10}, memberships,
        PlateHierarchy({}, {}), grid_start_ms=0, grid_end_ms=0,
    )
    row = result.cohesion[0]
    assert "INSUFFICIENT_MEMBERS" in row.flags
    assert "INSUFFICIENT_POINTS" in row.flags
    assert row.pct_median is None
    assert lead_lag([1, 1, 1], [2, 2, 2], min_points=2) == (None, None)


def test_insufficient_members_make_affinities_unknown_and_inactive():
    symbols = ("a", "b")
    memberships = _members(*symbols)
    series = {
        symbol: [(i * 3000, 10.0 + i, float(i)) for i in range(12)]
        for symbol in symbols
    }
    result = calculate_plate_cohesion(
        series, {symbol: 10.0 for symbol in symbols}, memberships, PlateHierarchy({}, {}),
        grid_start_ms=0, grid_end_ms=33000, min_points=2,
    )
    assert "INSUFFICIENT_MEMBERS" in result.cohesion[0].flags
    assert {row.role for row in result.affinities} == {"UNKNOWN"}
    assert {row.status for row in active_plates_for_stock(result.affinities)} == {"UNKNOWN"}


def test_amount_delta_up_and_down_shares_are_reported_separately():
    symbols = ("a", "b", "c", "d", "e")
    memberships = _members(*symbols)
    series = {symbol: [(i * 3000, 10.0, 100.0 - i) for i in range(12)] for symbol in symbols}
    result = calculate_plate_cohesion(
        series, {symbol: 10.0 for symbol in symbols}, memberships, PlateHierarchy({}, {}),
        grid_start_ms=0, grid_end_ms=33000, min_points=2,
    )
    row = result.cohesion[0]
    assert row.amount_delta_up_share == 0.0
    assert row.amount_delta_down_share == 1.0


def test_pre_close_is_asof_each_grid_point_and_change_is_flagged():
    symbols = ("a", "b", "c", "d", "e")
    memberships = _members(*symbols)
    series = {
        symbol: [(0, 10.0, 100.0, 10.0), (3000, 20.0, 100.0, 20.0)]
        for symbol in symbols
    }
    result = calculate_plate_cohesion(
        series, {symbol: 10.0 for symbol in symbols}, memberships, PlateHierarchy({}, {}),
        grid_start_ms=0, grid_end_ms=3000, min_members=1, min_points=1,
    )
    grid = align_asof_grid(series, 0, 3000)
    assert grid["a"] == [(0, 10.0, 100.0, 10.0), (3000, 20.0, 100.0, 20.0)]
    assert "pc_changed_in_window" in result.cohesion[0].flags
    # Each price is unchanged relative to the pre-close known at its grid point.
    assert result.cohesion[0].pct_median == 0.0
    assert result.cohesion[0].speed_median == 0.0


def test_noise_and_event_plates_are_skipped():
    memberships = build_memberships(
        symbols=("a",), pool_plate_by_symbol={"a": ["人工智能", "回购", "2026年公告"]},
        reason_candidates_by_symbol={}, static_themes_by_symbol={},
    )
    result = calculate_plate_cohesion(
        {"a": [(i * 3000, 10 + i, i) for i in range(12)]}, {"a": 10}, memberships,
        PlateHierarchy({}, {}), grid_start_ms=0, grid_end_ms=33000,
    )
    assert [row.plate for row in result.cohesion] == ["人工智能"]
