from engine_core.auction_baseline import build_auction_baseline


def test_baseline_uses_only_final_0925_amounts_and_keeps_market_and_symbol_history():
    rows = [
        {"trade_date": "20261013", "auction_tag": "0925", "symbol": "600001", "match_amt_yuan": 100},
        {"trade_date": "20261013", "auction_tag": "0920", "symbol": "600001", "match_amt_yuan": 90},
        {"trade_date": "20261014", "auction_tag": "0925", "symbol": "600001", "match_amt_yuan": 300},
        {"trade_date": "20261014", "auction_tag": "0925", "symbol": "000002", "match_amt_yuan": 200},
        {"trade_date": "20261015", "auction_tag": "0925", "symbol": "600001", "match_amt_yuan": None},
    ]

    result = build_auction_baseline(rows, target_date="2026-10-16", window_days=64)

    market = [row for row in result["rows"] if row["row_kind"] == "market_observation"]
    assert [(row["date"], row["sum_yi"]) for row in market] == [
        ("2026-10-13", 0.000001), ("2026-10-14", 0.000005)]
    stock = [row for row in result["rows"] if row["row_kind"] == "symbol_observation"]
    assert {(row["date"], row["symbol"], row["auction_amount_yuan"]) for row in stock} == {
        ("2026-10-13", "600001", 100),
        ("2026-10-14", "600001", 300),
        ("2026-10-14", "000002", 200),
    }
    market_summary = result["market_summary"]
    assert market_summary["sample_days"] == 2
    assert market_summary["median_yuan"] == 300
    assert market_summary["p10_yuan"] == 140
    assert market_summary["p90_yuan"] == 460
    assert market_summary["missing_dates"] == ["2026-10-15"]
    symbol_summary = {row["symbol"]: row for row in result["symbol_summaries"]}
    assert symbol_summary["600001"]["median_yuan"] == 200
    assert symbol_summary["000002"]["sample_days"] == 1


def test_baseline_is_partial_when_window_has_missing_final_amounts():
    result = build_auction_baseline([
        {"trade_date": "20261015", "auction_tag": "0925", "symbol": "600001", "match_amt_yuan": None},
    ], target_date="2026-10-16", window_days=64)
    assert result["status"] == "PARTIAL"
    assert result["market_summary"]["sample_days"] == 0
    assert result["market_summary"]["missing_dates"] == ["2026-10-15"]


def test_auction_points_fills_market_total_without_filling_absent_symbols():
    result = build_auction_baseline([
        {"trade_date": "20261014", "auction_tag": "0925", "symbol": "600001", "match_amt_yuan": 500},
    ], target_date="2026-10-16", source_dates=["2026-10-14", "2026-10-15"],
       fallback_market={"2026-10-15": 700}, fallback_symbols=[
           {"trade_date": "2026-10-15", "symbol": "000002", "auction_amount_yuan": 300},
       ])
    market = {row["date"]: row for row in result["rows"] if row["row_kind"] == "market_observation"}
    assert market["2026-10-15"]["auction_amount_yuan"] == 700
    assert market["2026-10-15"]["source"] == "auction_points_summary"
    observations = {(row["date"], row["symbol"]) for row in result["rows"]
                    if row["row_kind"] == "symbol_observation"}
    assert observations == {("2026-10-14", "600001"), ("2026-10-15", "000002")}
