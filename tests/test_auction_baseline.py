# DATA: spec (hand-built rows for edge cases) + real:tests/fixtures/real/auction_baseline_20261106
import gzip
import json
from pathlib import Path

import engine_core.auction_baseline as auction_baseline
from engine_core.auction_baseline import build_auction_baseline
from engine_core.data_pipeline.storage import ArtifactCatalog


def _real_input():
    fixture = Path(__file__).parent / "fixtures/real/auction_baseline_20261106"
    expected = json.loads((fixture / "live_expected.json").read_text())
    with gzip.open(fixture / "td_rows.jsonl.gz", "rt") as stream:
        rows = [json.loads(line) for line in stream]
    return expected, rows


def test_real_td_statistics_match_live_artifact():
    expected, rows = _real_input()
    result = build_auction_baseline(rows, target_date="2026-11-06",
                                    source_dates=expected["td_dates"],
                                    available_at="2026-11-06T00:00:00+00:00")
    for key in ("median_yuan", "p10_yuan", "p25_yuan", "p75_yuan", "p90_yuan",
                "minimum_yuan", "maximum_yuan", "sample_days", "earliest_date", "latest_date"):
        assert result["market_summary"][key] == expected["live_manifest_market_summary"][key]


def test_real_td_manifest_counts_observed_dates(monkeypatch, tmp_path):
    expected, rows = _real_input()
    dates = expected["td_dates"]
    sessions = sorted(dates + expected["live_manifest_quality"]["missing_market_dates"])
    monkeypatch.setattr(auction_baseline, "_read_td_window", lambda target: (dates, rows))
    monkeypatch.setattr(auction_baseline, "_expected_sessions", lambda catalog, target, td: sessions)
    monkeypatch.setattr(auction_baseline, "_auction_points_fallback", lambda catalog, dates: ({}, []))
    auction_baseline.build_and_store_auction_baseline("2026-11-06", artifact_root=tmp_path, publish=False)
    manifest = json.loads(ArtifactCatalog(tmp_path).locate("auction_baseline", "2026-11-06").read_text())
    assert manifest["quality"]["observed_source_dates"] == 28
    assert len(manifest["quality"]["missing_market_dates"]) == 36


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
    assert all(market_summary[k] is None for k in ("median_yuan", "p10_yuan", "p90_yuan"))
    assert market_summary["n_obs"] == 2 and market_summary["warm"] is False and market_summary["quality"] == "PARTIAL"
    assert market_summary["missing_dates"] == ["2026-10-15"]
    symbol_summary = {row["symbol"]: row for row in result["symbol_summaries"]}
    assert symbol_summary["600001"]["median_yuan"] is None
    assert symbol_summary["600001"]["n_obs"] == 2 and not symbol_summary["600001"]["warm"] and symbol_summary["600001"]["quality"] == "PARTIAL"
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


def test_real_window_with_more_than_minimum_days_is_ready():
    expected, rows = _real_input()
    result = build_auction_baseline(rows, target_date="2026-11-06", source_dates=expected["td_dates"])
    assert result["status"] == "READY"


def test_real_window_minimum_and_symbol_coverage():
    expected, rows = _real_input()
    dates = sorted(expected["td_dates"])
    for count in (4, 5):
        selected = dates[-count:]
        result = build_auction_baseline(rows, target_date="2026-11-06", source_dates=selected)
        assert result["status"] == ("READY" if count == 5 else "PARTIAL")
        assert result["warm"] == (count == 5)
        assert (result["observed_days"], result["min_days"], result["window_days_max"]) == (count, 5, 64)
    selected = dates[-5:]
    symbol = next(str(r["symbol"]) for r in rows if str(r.get("trade_date", "")).replace("-", "") == selected[-1].replace("-", ""))
    limited = [r for r in rows if str(r.get("symbol")) != symbol or str(r.get("trade_date", "")).replace("-", "") in {d.replace("-", "") for d in selected[-3:]}]
    result = build_auction_baseline(limited, target_date="2026-11-06", source_dates=selected)
    row = next(r for r in result["symbol_summaries"] if r["symbol"] == symbol.zfill(6))
    assert row["n_obs"] == 3 and not row["warm"] and row["quality"] == "PARTIAL" and row["median_yuan"] is None
    assert result["status"] == "READY" and any(r["warm"] for r in result["symbol_summaries"])
