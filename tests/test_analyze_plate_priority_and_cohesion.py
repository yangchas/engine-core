import json

import pytest

from examples.analyze_plate_priority_and_cohesion import analyze, write_outputs


def test_priority_categories_and_tier_statistics():
    memberships = {
        "policy": "USER_PRIORITY_V1",
        "by_symbol": {
            "same": [{"plate": "静态板", "tier": 3, "rank_in_stock": 1}],
            "reason": [{"plate": "原因板", "tier": 2, "rank_in_stock": 1}],
            "generic": [{"plate": "机器人", "tier": 3, "rank_in_stock": 1, "generic": False}],
            "alias": [{"plate": "芯片", "tier": 2, "rank_in_stock": 1}],
            "other": [{"plate": "新板", "tier": 3, "rank_in_stock": 1}],
            "r2": [{"plate": "原因板", "tier": 2, "rank_in_stock": 1}],
            "r3": [{"plate": "原因板", "tier": 3, "rank_in_stock": 1}],
        },
    }
    snapshot = {"stock_plate": {
        "same": "静态板", "reason": "旧板", "generic": "昨日涨停", "alias": "HBM", "other": "旧板",
        "r2": "原因板", "r3": "原因板",
    }}
    affinity = [
        {"symbol": "r2", "plate": "原因板", "role": "LEADER", "best_corr": -0.8},
        {"symbol": "r3", "plate": "原因板", "role": "UNKNOWN", "best_corr": None},
    ]
    result = analyze(memberships, affinity, snapshot=snapshot)
    categories = result["priority_difference"]["categories"]
    assert {key: value["count"] for key, value in categories.items()} == {
        "SAME": 3, "REASON_OVER_STATIC": 1, "GENERIC_REORDER": 1,
        "ALIAS_COLLAPSE": 1, "OTHER": 1,
    }
    stats = result["reason_cohesion"]
    assert stats["tier_2"]["role_counts"] == {"LEADER": 1}
    assert stats["tier_2"]["abs_best_corr_median"] == 0.8
    assert stats["tier_3"]["role_counts"] == {"UNKNOWN": 1}
    assert stats["tier_3"]["unknown_count"] == 1


def test_missing_affinity_is_counted_without_zero_fill():
    result = analyze({"by_symbol": {"x": [{"plate": "原因板", "tier": 2, "rank_in_stock": 1}]}}, [])
    assert result["reason_cohesion"]["tier_2"]["membership_count"] == 1
    assert result["reason_cohesion"]["tier_2"]["affinity_count"] == 0
    assert result["reason_cohesion"]["tier_2"]["missing_affinity_count"] == 1
    assert result["reason_cohesion"]["tier_2"]["abs_best_corr_median"] is None


def test_missing_input_file_has_clear_error(tmp_path):
    with pytest.raises(ValueError, match="missing required input file: memberships.json"):
        analyze_from_dirs(tmp_path, tmp_path)


def analyze_from_dirs(memberships_dir, cohesion_dir):
    from examples.analyze_plate_priority_and_cohesion import load_inputs
    return load_inputs(memberships_dir, cohesion_dir)


def test_writer_rejects_existing_output_directory(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "keep").write_text("x")
    with pytest.raises(ValueError, match="empty"):
        write_outputs(output, {})
