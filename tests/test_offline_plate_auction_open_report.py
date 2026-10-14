import json

import pytest

from examples import run_offline_plate_auction_open_report as runner


def test_output_directory_must_be_empty(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "existing").write_text("x")
    with pytest.raises(ValueError, match="empty"):
        runner.ensure_empty_output_dir(output)


def test_runner_writes_report_and_manifest_with_stage_semantics(tmp_path, monkeypatch):
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps({"stock_plate": {"000001": "银行"}, "s2p": {}}))
    monkeypatch.setattr(runner, "_read_td", lambda *args: (
        {"0920": {}, "0924": {}, "0925": {}}, {}, {}, {"000001": []},
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
    assert "NOT_POINT_IN_TIME" in manifest["degradation"]
    report = (day / "report.md").read_text()
    assert report.startswith("标签: NOT_POINT_IN_TIME")
    assert manifest["degradation"] == ["KAIPAN_INPUTS_MISSING", "NOT_POINT_IN_TIME"]
