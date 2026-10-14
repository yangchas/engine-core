import csv
import json

import pytest

from engine_core.daily_kline_capture import parse_row, write_capture
from examples.run_extremes_review import main


def test_cli_builds_outputs_reads_history_and_rejects_nonempty_output(tmp_path):
    report = tmp_path / "report"
    report.mkdir()
    (report / "stocks.json").write_text(json.dumps({"stocks": [
        {"symbol": "600001", "name": "甲", "primary_plate": "通信", "in_yest_pool": True, "lb_days": 2},
        {"symbol": "300001", "name": "乙", "primary_plate": "通信", "in_yest_pool": False},
    ]}), encoding="utf-8")
    (report / "plates.json").write_text("[]", encoding="utf-8")
    (report / "plate_stock_view.json").write_text("{}", encoding="utf-8")
    (report / "manifest.json").write_text("{}", encoding="utf-8")

    checkpoints = tmp_path / "checkpoints.csv"
    with checkpoints.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("date", "tag", "cutoff", "symbol", "last_tick_ts", "px", "pre_close", "amt_yuan_cum", "bp1", "ap1", "bv1", "av1"))
        writer.writeheader()
        for symbol, auction, final_ts in (("600001", "102", "2026-10-14T09:25:01+08:00"), ("300001", "98", "2026-10-14T09:24:57+08:00")):
            for tag, px, ts in (("0925", auction, final_ts), ("0932", "103", "2026-10-14T09:32:00+08:00"), ("0945", "104", "2026-10-14T09:45:00+08:00")):
                writer.writerow({"date": "2026-10-14", "tag": tag, "cutoff": tag, "symbol": symbol, "last_tick_ts": ts, "px": px, "pre_close": "100"})
    auction = tmp_path / "auction.csv"
    with auction.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("trade_date", "auction_tag", "ts", "symbol", "px_milli", "chg_bp", "match_amt_yuan", "rest_bid_amt_yuan", "rest_ask_amt_yuan", "limit_state"))
        writer.writeheader()
        writer.writerows([
            {"trade_date": "2026-10-14", "auction_tag": "0925", "ts": "09:25:06", "symbol": "600001", "chg_bp": "200", "match_amt_yuan": "1000000", "rest_bid_amt_yuan": "100", "rest_ask_amt_yuan": "0", "limit_state": "0"},
            {"trade_date": "2026-10-14", "auction_tag": "0925", "ts": "09:25:06", "symbol": "300001", "chg_bp": "-200", "match_amt_yuan": "500000", "rest_bid_amt_yuan": "0", "rest_ask_amt_yuan": "100", "limit_state": "0"},
        ])
    daily = tmp_path / "daily"
    daily.mkdir()
    rows = [parse_row({"symbol": "600001", "date": "2026-10-14", "open": 102, "high": 108, "low": 98, "close": 107, "pre_close": 100, "volume": 10, "amount_yuan": 100}),
            parse_row({"symbol": "300001", "date": "2026-10-14", "open": 98, "high": 102, "low": 95, "close": 101, "pre_close": 100, "volume": 10, "amount_yuan": 100})]
    write_capture(daily, rows, {"date": "2026-10-14", "valid": 2})
    history = tmp_path / "history"
    history.mkdir()
    (history / "2026-10-13.json").write_text(json.dumps({"trade_date": "2026-10-13", "summary": {"close_median": 0, "close_up_rate": .5, "delta_quantiles": {"p50": 0, "p5": -1, "p95": 1}, "left_tail_delta_le_minus_8": 1, "right_tail_delta_ge_8": 0}, "diagnostics": {}, "plates": {}, "stocks": [], "bins": {}}), encoding="utf-8")
    out = tmp_path / "out"
    args = ["--trade-date", "2026-10-14", "--report-dir", str(report), "--checkpoints-csv", str(checkpoints),
            "--auction-snapshot-csv", str(auction), "--daily-capture-dir", str(daily), "--history-dir", str(history), "--out-dir", str(out)]
    assert main(args) == 0
    result = json.loads((out / "extremes.json").read_text(encoding="utf-8"))
    assert result["history"]["sample_days"] == 2
    assert result["summary"]["indicative_count"] == 1
    assert (out / "extremes_review.md").exists() and (out / "manifest.json").exists()
    with pytest.raises(FileExistsError):
        main(args)
