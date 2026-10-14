import json

import pytest

from examples.run_plate_peer_view import main


def test_cli_writes_outputs_and_rejects_nonempty_out_dir(tmp_path):
    report = tmp_path / "report"
    report.mkdir()
    (report / "stocks.json").write_text(json.dumps([
        {"symbol": "000001", "name": "A", "plates": [{"plate": "传媒"}]},
        {"symbol": "000002", "name": "B", "plates": [{"plate": "传媒"}]},
        {"symbol": "000003", "name": "C", "plates": [{"plate": "传媒"}]},
        {"symbol": "000004", "name": "D", "plates": [{"plate": "传媒"}]},
        {"symbol": "000005", "name": "E", "plates": [{"plate": "传媒"}]},
    ]), encoding="utf-8")
    (report / "plates.json").write_text("[]", encoding="utf-8")
    (report / "plate_stock_view.json").write_text("{}", encoding="utf-8")
    checkpoints = tmp_path / "checkpoints.csv"
    checkpoints.write_text(
        "date,tag,cutoff,symbol,last_tick_ts,px,pre_close,amt_yuan_cum,bp1,ap1,bv1,av1\n"
        "2026-10-15,0932,09:32:00,000002,09:32:00,10.5,10,100,0,0,0,0\n"
        "2026-10-15,0945,09:45:00,000002,09:45:00,11,10,200,0,0,0,0\n",
        encoding="utf-8")
    auction = tmp_path / "auction.csv"
    auction.write_text(
        "trade_date,auction_tag,ts,symbol,px_milli,chg_bp,match_amt_yuan,rest_bid_amt_yuan,rest_ask_amt_yuan,limit_state\n"
        "2026-10-15,0925,09:25:06,000001,11000,1000,100000,50000,0,1\n"
        "2026-10-15,0925,09:25:06,000002,10100,100,50000,0,0,0\n"
        "2026-10-15,0925,09:25:06,000003,10200,200,50000,0,0,0\n"
        "2026-10-15,0925,09:25:06,000004,10300,300,50000,0,0,0\n"
        "2026-10-15,0925,09:25:06,000005,10400,400,50000,0,0,0\n",
        encoding="utf-8")
    history = tmp_path / "daily.csv"
    lines = ["date,symbol,open,high,low,close,volume,amount_yuan,turnover_pct,pct_chg"]
    for day in range(1, 24):
        date = f"2026-10-{day:02d}"
        for symbol, close in (("000001", 10), ("000002", 9 + day / 10), ("000003", 10),
                              ("000004", 10), ("000005", 10)):
            lines.append(f"{date},{symbol},{close},{close + .1},{close - .1},{close},100,100,1,0")
    history.write_text("\n".join(lines) + "\n", encoding="utf-8")
    relation = tmp_path / "relations"
    relation.mkdir()
    (relation / "registry.json").write_text(json.dumps({"plates": [
        {"code": "801001", "name": "传媒", "class": "THEME"}]}), encoding="utf-8")
    (relation / "relation.csv").write_text(
        "plateid,stockid\n" + "".join(f"801001,{symbol}\n" for symbol in
                                        ("000001", "000002", "000003", "000004", "000005")),
        encoding="utf-8")
    generic = tmp_path / "generic.json"
    generic.write_text("[]", encoding="utf-8")
    out = tmp_path / "out"
    args = ["--trade-date", "2026-10-15", "--report-dir", str(report),
            "--checkpoints-csv", str(checkpoints), "--auction-snapshot-csv", str(auction),
            "--daily-history-csv", str(history), "--relation-dir", str(relation),
            "--generic-names-json", str(generic), "--out-dir", str(out)]
    assert main(args) == 0
    payload = json.loads((out / "plate_peer_view.json").read_text(encoding="utf-8"))
    assert payload["manifest"]["a_count"] >= 1
    assert payload["manifest"]["plate_count"] == 1
    assert payload["manifest"]["peer_count"] == 4
    assert payload["plates"][0]["auction_mean_pct"] == 4.0
    assert payload["a_list"][0]["buyable"] is False
    assert "虚拟撮合" in (out / "plate_peer_view.md").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        main(args)
