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


def test_comparison_reports_seal_evidence_and_excluded_unsealed_limit_prices():
    result = compare_pools(
        {"600001": [_row("2026-10-12")], "600002": [_row("2026-10-12")]},
        [], "2026-10-12", close_limit_state_by_symbol={"600001": 1, "600002": 0},
    )
    assert result["core_seal_verified_count"] == 1
    assert result["core_no_seal_evidence_count"] == 0
    assert result["excluded_limit_price_without_seal"] == ["600002"]
