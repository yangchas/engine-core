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


def test_three_way_reconciliation_and_legacy_output_keys():
    memberships = {
        "by_symbol": {
            "same": [{"plate": "芯片", "tier": 3, "rank_in_stock": 1}],
            "bracket": [{"plate": "芯片", "tier": 3, "rank_in_stock": 1}],
            "alias": [{"plate": "芯片", "tier": 2, "rank_in_stock": 1}],
            "generic": [{"plate": "昨日涨停", "tier": 1, "rank_in_stock": 1}],
            "reason": [{"plate": "原因板", "tier": 2, "rank_in_stock": 1}],
        },
        "legacy_weights": {
            "same": [["芯片", 1.0]], "bracket": [["先进封装", 1.0]],
            "alias": [["芯片", 1.0]], "generic": [["芯片", 1.0]],
            "reason": [["候选板", 1.0]],
        },
    }
    snapshot = {"stock_plate": {
        "same": "芯片/通信", "bracket": "芯片（半导体）", "alias": "先进封装",
        "generic": "昨日涨停", "reason": "静态板",
    }}

    result = analyze(memberships, [], snapshot=snapshot)
    reconciliation = result["reconciliation"]
    assert reconciliation["n_comparable"] == 5
    assert reconciliation["stored_eq_legacy_v1_share"] == 1 / 5
    assert reconciliation["stored_eq_user_share"] == 3 / 5
    assert reconciliation["legacy_v1_eq_user_share"] == 2 / 5
    assert reconciliation["stored_vs_legacy_v1_reason_counts"]["BRACKET_CLEAN"] == 1
    assert reconciliation["stored_vs_legacy_v1_reason_counts"]["ALIAS"] == 1
    assert reconciliation["stored_vs_legacy_v1_reason_counts"]["GENERIC"] == 1
    assert reconciliation["stored_vs_legacy_v1_reason_counts"]["POOL_OR_REASON"] == 1
    assert "comparable_count" in result["priority_difference"]
    assert "categories" in result["priority_difference"]


def test_reconciliation_excludes_missing_symbols_and_reports_count():
    memberships = {
        "by_symbol": {
            "all": [{"plate": "芯片", "rank_in_stock": 1}],
            "no_legacy": [{"plate": "通信", "rank_in_stock": 1}],
            "no_user": [{"plate": "医药", "rank_in_stock": 2}],
            "no_stored": [{"plate": "汽车", "rank_in_stock": 1}],
        },
        "legacy_weights": {"all": [["芯片", 1]], "no_legacy": [["通信", 1]],
                            "no_user": [["医药", 1]], "no_stored": [["汽车", 1]]},
    }
    result = analyze(memberships, [], snapshot={"stock_plate": {
        "all": "芯片", "no_user": "医药", "no_stored": None,
    }})
    reconciliation = result["reconciliation"]
    assert reconciliation["n_comparable"] == 1
    assert reconciliation["excluded_missing_counts"] == {
        "stored_legacy_primary_missing": 2,
        "legacy_v1_primary_missing": 0,
        "user_priority_rank1_missing": 1,
    }


def test_legacy_primary_prefers_direct_pipeline_key():
    from examples.analyze_plate_priority_and_cohesion import _legacy_primary_by_symbol

    assert _legacy_primary_by_symbol({"legacy_v1_primary_by_symbol": {"000001": "芯片(设备)"}}) == {"000001": "芯片"}
