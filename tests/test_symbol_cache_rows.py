from engine_core.symbol_cache_rows import normalize_symbol_cache_rows


def test_hot_rank_skips_rank_less_rows_and_keeps_missing_heat_none():
    result = normalize_symbol_cache_rows("hot_rank", "2026-10-12", [
        {"symbol": "000001", "rank": 1, "heat": 9.5, "name": "A", "trade_date": "2026-10-12", "source": "ths"},
        {"symbol": "000002", "rank": 2, "trade_date": "2026-10-12"},
        {"symbol": "000003", "rank": None, "trade_date": "2026-10-12"},
        {"symbol": "000004", "rank": 4, "trade_date": "2026-10-11"},
        "bad",
    ])
    by = {r["symbol"]: r for r in result.rows}
    assert set(by) == {"000001", "000002"}
    assert by["000002"]["heat"] is None
    assert (result.invalid_row_count, result.date_mismatch_count) == (2, 1)
    assert result.status == "PARTIAL"


def test_limit_truth_ready_and_duplicate_reported():
    rows = [{"symbol": "600000", "lb_days": 2, "trade_date": "2026-10-12", "source": "wencai"}]
    assert normalize_symbol_cache_rows("limit_truth", "2026-10-12", rows).status == "READY"
    dup = normalize_symbol_cache_rows("limit_truth", "2026-10-12", rows + rows)
    assert dup.duplicate_symbols == ("600000",) and dup.status == "PARTIAL"
    assert normalize_symbol_cache_rows("limit_truth", "2026-10-12", []).status == "MISSING"
