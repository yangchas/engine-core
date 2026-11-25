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
        "n_conflicting_open": 0,
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


def test_compare_cli_reports_conflicting_opens_and_excludes_them(tmp_path, monkeypatch, capsys):
    import json
    import sys

    from examples import compare_task008_recovered_anchor_vs_kline_open as module

    anchor_path = tmp_path / "anchors.json"
    anchor_path.write_text(json.dumps({
        "600001": {"a25": 10000, "src": "a25"},
        "600002": {"a25": 20000, "src": "a25"},
    }), encoding="utf-8")
    first_path = tmp_path / "opens-1.json"
    first_path.write_text(json.dumps({"open_by_symbol": {"600001": 10.0, "600002": 20.0}}), encoding="utf-8")
    second_path = tmp_path / "opens-2.json"
    second_path.write_text(json.dumps({"open_by_symbol": {"600001": 11.0, "600002": 20.0}}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["compare", "--anchor-table", str(anchor_path),
                                      "--open-json", str(first_path), str(second_path)])

    assert module.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["conflicting_open_symbols"] == ["600001"]
    assert result["a25"]["n_conflicting_open"] == 1
    assert result["a25"]["n_with_open"] == 1
    assert result["a25"]["n_equal"] == 1
    assert result["a25"]["equal_rate"] == 1.0


def test_compare_counts_exact_tolerance_boundary_as_equal():
    result = compare({"600001": {"a25": 10010, "src": "a25"}},
                     {"600001": 10.0}, tol=0.01)

    assert result["a25"]["n_equal"] == 1
    assert result["a25"]["n_not_equal"] == 0


def test_compare_cli_checks_expected_trade_date_and_documents_open_provenance(tmp_path, monkeypatch, capsys):
    import json
    import sys

    from examples import compare_task008_recovered_anchor_vs_kline_open as module

    assert "恢复价应与交易日当日的日K open 比较(open-json 来自" in module.__doc__
    assert "run_real_previous_day_stats --previous-trade-date 为该交易日" in module.__doc__
    anchor_path = tmp_path / "anchors.json"
    anchor_path.write_text("{}", encoding="utf-8")
    open_path = tmp_path / "opens.json"
    open_path.write_text(json.dumps({"previous_trade_date": "2026-10-09",
                                     "open_by_symbol": {}}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["compare", "--anchor-table", str(anchor_path),
                                      "--open-json", str(open_path),
                                      "--expect-trade-date", "2026-10-10"])

    assert module.main() == 2
    assert "previous_trade_date 2026-10-09 does not match expected 2026-10-10" in capsys.readouterr().out
