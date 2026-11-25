import json

import pytest

from engine_core.plate_membership import PlateHierarchy, build_memberships
from examples.evaluate_plate_aggregation import evaluate_day, ensure_empty_output_dir


def _memberships(plates, *, legacy=False):
    return build_memberships(
        symbols=plates,
        pool_plate_by_symbol={},
        reason_candidates_by_symbol={},
        static_themes_by_symbol=plates,
        policy="LEGACY_V1" if legacy else "USER_PRIORITY_V1",
    )


def test_evaluate_day_excess_and_spearman_are_numerically_correct():
    memberships = _memberships({"a": ["甲板"], "b": ["乙板"], "c": ["丙板"]})
    rows = {
        "a": {"px_milli": 10000, "chg_bp": 100, "match_amt_yuan": 300, "limit_state": 0},
        "b": {"px_milli": 10000, "chg_bp": 0, "match_amt_yuan": 200, "limit_state": 0},
        "c": {"px_milli": 10000, "chg_bp": -100, "match_amt_yuan": 100, "limit_state": 0},
    }
    closes = {"a": {"2026-01-02": 10.1, "2026-01-05": 10.2},
              "b": {"2026-01-02": 10.0, "2026-01-05": 10.1},
              "c": {"2026-01-02": 9.9, "2026-01-05": 10.0}}
    result = evaluate_day(rows, closes, memberships, PlateHierarchy({}, {}), "2026-01-02", "2026-01-05", min_members=1)
    item = result["rankings"]["core_excess_pct"]
    assert item["top_plates"][0]["plate"] == "甲板"
    assert item["same_day"]["mean_excess_pct"] == pytest.approx(0.0)
    assert item["next_day"]["mean_excess_pct"] == pytest.approx(0.0)
    assert item["spearman_rank_vs_same_day_excess"] == pytest.approx(1.0)


def test_generic_plates_excluded_and_missing_closes_counted():
    memberships = _memberships({"a": ["甲板", "其他概念"], "b": ["甲板"], "c": ["其他概念"]})
    rows = {s: {"px_milli": 10000, "chg_bp": 10, "match_amt_yuan": 100, "limit_state": 0}
            for s in ("a", "b", "c")}
    closes = {"a": {"2026-01-02": 10.1}, "b": {"2026-01-02": 10.2}}
    result = evaluate_day(rows, closes, memberships, PlateHierarchy({}, {}), "2026-01-02", "2026-01-05", min_members=1)
    assert all(row["plate"] != "其他概念" for row in result["rankings"]["core_amount_share"]["top_plates"])
    assert result["missing_counts"]["same_day_close"] == 1
    assert result["missing_counts"]["next_day_close"] == 3


def test_legacy_weights_use_one_point_zero_point_six_and_generic_reduction():
    memberships = build_memberships(
        symbols=["a"], pool_plate_by_symbol={"a": ["甲板"]},
        reason_candidates_by_symbol={}, static_themes_by_symbol={"a": ["昨日涨停", "乙板"]},
        policy="LEGACY_V1",
    )
    row = {"px_milli": 10000, "chg_bp": 0, "match_amt_yuan": 100, "limit_state": 0}
    result = evaluate_day({"a": row}, {"a": {"2026-01-02": 10}}, memberships,
                          PlateHierarchy({}, {}), "2026-01-02", "2026-01-05", min_members=1)
    scores = result["ranking_scores"]["legacy_amount_weighted"]
    assert scores["甲板"] == pytest.approx(100)
    assert scores["昨日涨停"] == pytest.approx(10.8)


def test_output_directory_must_be_empty(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "existing.json").write_text("{}")
    with pytest.raises(ValueError, match="empty"):
        ensure_empty_output_dir(out)
