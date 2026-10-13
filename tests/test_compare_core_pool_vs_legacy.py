from examples.compare_task008_core_pool_vs_legacy import compare_pools


def _row(day, pct=10.0):
    return [day + " 15:00:00", 10, 11, 9, 11, 10, 100, 1, pct]


def test_comparison_reports_recall_precision_and_lb_days_difference():
    result = compare_pools(
        {
            "600001": [_row("2026-10-09"), _row("2026-10-12")],
            "600002": [_row("2026-10-12")],
        },
        [
            {"symbol": "600001", "lb_days": 1, "close_pct": 10},
            {"symbol": "600003", "lb_days": 1, "close_pct": None},
        ],
        "2026-10-12",
    )
    assert (result["n_core"], result["n_legacy"], result["both"]) == (2, 2, 1)
    assert result["recall"] == 0.5
    assert result["precision"] == 0.5
    assert result["lb_days_diff"] == [{"symbol": "600001", "core": 2, "legacy": 1}]
    assert result["legacy_only"][0]["close_pct"] is None
