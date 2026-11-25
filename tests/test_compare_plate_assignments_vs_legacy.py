from examples.compare_task008_plate_assignments_vs_legacy import compare_assignments
from engine_core.stock_plate_assignment import build_stock_plate_assignments


def test_comparison_counts_alias_generic_order_content_and_missing():
    core = build_stock_plate_assignments(
        trade_date="2026-10-13",
        static_primary_by_symbol={"1": "芯片", "2": "国企改革", "3": "通信"},
        static_themes_by_symbol={"1": ["芯片", "光纤"], "2": ["国企改革"], "3": ["通信", "芯片"]},
    )
    result = compare_assignments(
        core,
        {"stock_plate": {"1": "玻璃基板", "2": "通信", "3": "通信", "4": "芯片"},
         "s2p": {"1": ["芯片", "光纤"], "2": ["通信"], "3": ["芯片", "通信"], "4": ["芯片"]}},
    )
    assert result["n_compared"] == 3
    assert result["primary_equal"] == 1
    assert result["themes_equal"] == 1
    assert result["diff_counts"] == {
        "primary_diff_generic": 1,
        "primary_diff_alias": 1,
        "themes_order_only": 1,
        "themes_content": 1,
        "missing_in_core": 1,
        "missing_in_legacy": 0,
    }
