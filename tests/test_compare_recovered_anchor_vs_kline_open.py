from examples.compare_task008_recovered_anchor_vs_kline_open import compare


def test_compare_splits_sources_and_counts_equal_unequal_and_missing_open():
    anchor_table = {
        "600001": {"a25": 100000, "src": "a25"},
        "600002": {"a25": 101000, "src": "a25"},
        "600003": {"a25": 99000, "src": "a25"},
        "000001": {"a25": 20000, "src": "ph0_recovered"},
        "000002": {"a25": 21000, "src": "ph0_recovered"},
        "000003": {"a25": None, "src": "ph0_recovered"},
    }
    before = repr(anchor_table)

    result = compare(anchor_table, {"600001": 100.004, "600002": 102.0,
                                    "000001": 20.0}, tol=0.006)

    assert result["a25"] == {
        "n_with_anchor": 3,
        "n_with_open": 2,
        "n_equal": 1,
        "n_not_equal": 1,
        "n_no_open": 1,
        "equal_rate": 0.5,
        "worst_differences": [
            {"symbol": "600002", "a25_yuan": 101.0, "open_yuan": 102.0, "diff": -1.0}
        ],
    }
    assert result["ph0_recovered"]["n_with_anchor"] == 2
    assert result["ph0_recovered"]["n_with_open"] == 1
    assert result["ph0_recovered"]["n_equal"] == 1
    assert result["ph0_recovered"]["n_no_open"] == 1
    assert result["ph0_recovered"]["equal_rate"] == 1.0
    assert repr(anchor_table) == before


def test_compare_equal_rate_is_none_without_open_denominator():
    result = compare({"600001": {"a25": 100000, "src": "a25"}}, {})

    assert result["a25"]["n_with_anchor"] == 1
    assert result["a25"]["n_with_open"] == 0
    assert result["a25"]["n_no_open"] == 1
    assert result["a25"]["n_not_equal"] == 0
    assert result["a25"]["equal_rate"] is None
