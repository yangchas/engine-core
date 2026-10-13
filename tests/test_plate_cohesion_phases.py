from datetime import datetime

import pytest

from engine_core.plate_cohesion_phases import compute_phase_cohesion
from engine_core.plate_membership import PlateHierarchy, build_memberships


def ms(clock):
    return int(datetime.fromisoformat(f"2026-10-13T{clock}+08:00").timestamp() * 1000)


def setup():
    symbols = [f"00000{i}" for i in range(1, 6)]
    memberships = build_memberships(symbols=symbols, pool_plate_by_symbol={},
                                    reason_candidates_by_symbol={},
                                    static_themes_by_symbol={s: ["人工智能"] for s in symbols})
    return symbols, memberships, PlateHierarchy({}, {})


def test_auction_ignores_tick_amount_and_uses_snapshot_progress():
    symbols, memberships, hierarchy = setup()
    series = {s: [(ms("09:20:03"), 10.0 + i / 100, 0.0, 10.0) for i, s in enumerate(symbols)]
              for s in symbols}
    snapshots = {"0920": {s: 10.0 for s in symbols}, "0924": {s: 20.0 for s in symbols},
                 "0925": {s: 15.0 for s in symbols}}
    result = compute_phase_cohesion(series, {s: 10.0 for s in symbols}, memberships, hierarchy,
                                    amount_snapshots=snapshots, phases=("AUCTION",), min_members=1,
                                    min_points=1)
    row = result.by_phase["AUCTION"][0]
    assert row.amount_progress == {"0920": 50.0, "0924": 100.0, "0925": 75.0}
    assert row.amount_delta_up_share == 0.5


def test_open_amount_delta_uses_first_open_observation_as_baseline():
    symbols, memberships, hierarchy = setup()
    series = {s: [(ms("09:25:00"), 99.0, 2000.0, 10.0),
                  (ms("09:30:03"), 10.1, 1000.0, 10.0),
                  (ms("09:30:06"), 10.2, 1020.0, 10.0)] for s in symbols}
    result = compute_phase_cohesion(series, {s: 10.0 for s in symbols}, memberships, hierarchy,
                                    phases=("OPEN",), min_members=1, min_points=1, grid_ms=3000)
    row = result.by_phase["OPEN"][0]
    assert row.amount_delta_up_share == 1.0
    assert row.pct_median == pytest.approx(2.0)
    assert result.flags["gap"] == "GAP"
    assert result.flags["gap_skipped_points"] > 0


def test_lag_never_pairs_across_gap_and_call_result_is_insufficient():
    symbols, memberships, hierarchy = setup()
    series = {s: [(ms("09:24:57"), 10.0, 0.0, 10.0),
                  (ms("09:30:03"), 10.1, 100.0, 10.0)] for s in symbols}
    result = compute_phase_cohesion(series, {s: 10.0 for s in symbols}, memberships, hierarchy,
                                    phases=("CALL_RESULT", "OPEN"), min_members=1, min_points=3)
    assert "INSUFFICIENT_POINTS" in result.by_phase["CALL_RESULT"][0].flags
    assert all(row.phase == "OPEN" for row in result.affinities_by_phase["OPEN"])

