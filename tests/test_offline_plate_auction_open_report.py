import json
from datetime import datetime

import pytest

from examples import run_offline_plate_auction_open_report as runner


def test_output_directory_must_be_empty(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "existing").write_text("x")
    with pytest.raises(ValueError, match="empty"):
        runner.ensure_empty_output_dir(output)


def test_eod_conclusion_is_rejected_by_session_report_cli():
    with pytest.raises(SystemExit):
        runner.main(["--trade-date", "2026-10-13", "--snapshot", "snapshot.json",
                     "--kline-range", "2026-10-12:2026-10-13", "--output-dir", "out",
                     "--conclusion-phase", "EOD"])


def test_same_day_snapshot_is_not_point_in_time_but_previous_day_is():
    same_day = runner._point_in_time_flags("2026-10-13", "2026-10-13", {
        "0920": "2026-10-13T09:20:00", "0924": "2026-10-13T09:24:00",
        "0925": "2026-10-13T09:25:00", "0932": "2026-10-13T09:32:00",
    })
    prior_day = runner._point_in_time_flags("2026-10-12", "2026-10-13", {
        "0920": "2026-10-13T09:20:00", "0924": "2026-10-13T09:24:00",
        "0925": "2026-10-13T09:25:00", "0932": "2026-10-13T09:32:00",
    })
    assert same_day == ["0920", "0924", "0925", "0932"]
    assert prior_day == []


def test_eod_pipeline_artifact_is_rejected(tmp_path):
    (tmp_path / "summary.json").write_text(json.dumps({"conclusion_phase": "EOD"}))
    with pytest.raises(ValueError, match="EOD"):
        runner._reject_eod_pipeline_product(tmp_path)


def test_runner_writes_report_and_manifest_with_stage_semantics(tmp_path, monkeypatch):
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps({"stock_plate": {"000001": "银行"}, "s2p": {}}))
    monkeypatch.setattr(runner, "_read_td", lambda *args: (
        {"0920": {}, "0924": {}, "0925": {}}, {}, {}, {"000001": []},
        {"0920": [], "0924": [], "0925": []}, {"read_s": 0.1, "build_s": 0.2, "total_s": 0.3, "query_count": 8},
    ))
    output = tmp_path / "out"
    runner.run_report(trade_dates=["2026-10-13"], snapshot_path=snapshot_path, kaipan_dir=None,
                      kline_range="2026-10-12:2026-10-13", td_host="unused", output_dir=output)
    day = output / "2026-10-13"
    assert {path.name for path in day.iterdir()} == {"stocks.json", "plates.json", "report.md", "manifest.json"}
    manifest = json.loads((day / "manifest.json").read_text())
    assert manifest["stage_semantics"]["0925"] == "竞价金额为虚拟匹配额"
    assert manifest["stage_semantics"]["0932"] == "真实累计成交额"
    assert manifest["reason_asof_rule"] == "Date<D"
    assert manifest["no_close_or_after_inputs"] is True
    assert manifest["query_count"] == 8
    assert set(manifest["timing"]) == {"read_s", "build_s", "total_s"}
    assert set(manifest["input_cutoff"]) == {"0920", "0924", "0925", "0932"}
    assert "NOT_POINT_IN_TIME" in manifest["degradation"]
    report = (day / "report.md").read_text()
    assert report.startswith("标签: NOT_POINT_IN_TIME")
    assert "| 0920 (NOT_POINT_IN_TIME) |" in report
    assert manifest["degradation"] == ["KAIPAN_INPUTS_MISSING", "NOT_POINT_IN_TIME"]


class _Cursor:
    def __init__(self):
        self.sql = ""
        self.statements = []

    def execute(self, sql):
        self.sql = sql
        self.statements.append(sql)

    def fetchall(self):
        sql = self.sql
        if "auction_snapshot_v2" in sql and "SELECT ts" in sql:
            return []
        if "auction_snapshot_v2" in sql:
            return []
        if "stock_tick_v2" in sql:
            return [
                (800000, 120000, datetime(2026, 10, 13, 9, 29, 0), 800000, 800000,
                 200, 100, 10, 3, "000001"),
                (125000, 1500000, datetime(2026, 10, 13, 9, 32, 0), 0, 0,
                 0, 0, 0, 0, "000002"),
            ]
        if "FROM market_data1.daily_kline" in sql:
            return [
                (datetime(2026, 10, 12), 10, 11, 9, 10.5, 0, 1, 1, 5, "d_000001"),
                (datetime(2026, 10, 13), 11, 12, 10, 11.5, 0, 2, 2, 9, "d_000001"),
                (datetime(2026, 10, 10), 20, 21, 19, 20.5, 0, 3, 3, 2, "d_000002"),
            ]
        raise AssertionError(f"unexpected query: {sql}")


class _Connection:
    def __init__(self):
        self.cursor_value = _Cursor()

    def cursor(self):
        return self.cursor_value

    def close(self):
        pass


@pytest.mark.parametrize("symbol_count", [2, 200])
def test_read_td_query_count_does_not_scale_with_symbols(monkeypatch, symbol_count):
    connection = _Connection()
    monkeypatch.setattr(runner, "_td_connect", lambda _host: (connection, "market_data1"))
    symbols = [f"{index:06d}" for index in range(symbol_count)]
    result = runner._read_td("unused", "2026-10-13", symbols, "2026-10-01:2026-10-13")
    assert result[-1]["query_count"] == len(connection.cursor_value.statements)
    assert result[-1]["query_count"] <= 10
    tick_query = next(sql for sql in connection.cursor_value.statements if "stock_tick_v2" in sql)
    assert "PARTITION BY symbol" in tick_query
    assert "symbol IN" not in tick_query
    assert "09:32:00" in tick_query
    assert result[1]["000001"]["px_milli"] == 800000
    assert result[1]["000001"]["flags"] == ["OPEN_SLICE_BEFORE_0930"]
    assert result[1]["000002"]["px_milli"] == 125000
    assert result[1]["000002"]["ts"].endswith("09:32:00+08:00")


def test_daily_kline_super_table_groups_preclose_and_keeps_missing_symbol_none(monkeypatch):
    connection = _Connection()
    monkeypatch.setattr(runner, "_td_connect", lambda _host: (connection, "market_data1"))
    auctions, opening, pre_close, klines, _baseline, _metrics = runner._read_td(
        "unused", "2026-10-13", ["000001", "000002", "000003"], "2026-10-01:2026-10-13",
    )
    daily_query = next(sql for sql in connection.cursor_value.statements if "daily_kline" in sql)
    assert "FROM market_data1.daily_kline" in daily_query
    assert "tbname" in daily_query
    assert len([sql for sql in connection.cursor_value.statements if "daily_kline" in sql]) == 1
    assert pre_close == {"000001": 10.5, "000002": 20.5, "000003": None}
    assert len(klines["000001"]) == 1
    assert klines["000003"] == []
