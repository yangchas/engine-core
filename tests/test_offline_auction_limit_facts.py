import json

import pytest

from examples import run_offline_auction_limit_facts as runner
from examples.run_offline_auction_limit_facts import _prior_values, main


def test_output_directory_must_be_empty(tmp_path):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"stock_plate": {}, "s2p": {}}))
    output = tmp_path / "output"
    output.mkdir()
    (output / "keep.txt").write_text("existing")
    with pytest.raises(ValueError, match="output directory must be empty"):
        main(["--trade-date", "2026-10-14", "--snapshot", str(snapshot),
              "--kline-range", "2026-10-13:2026-10-14", "--output-dir", str(output)])


def test_prior_kline_values_exclude_trade_day_and_future_rows():
    rows = [
        ["2026-10-13 15:00:00", 0, 0, 0, 10.0, 0, 1000.0],
        ["2026-10-14 15:00:00", 0, 0, 0, 99.0, 0, 99000.0],
        ["2026-10-15 15:00:00", 0, 0, 0, 88.0, 0, 88000.0],
    ]
    assert _prior_values(rows, "2026-10-14") == (10.0, 1000.0)


def test_limit_facts_manifest_records_snapshot_point_in_time_status(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"stock_plate": {"000001": "银行"}, "s2p": {}}))
    monkeypatch.setattr(runner, "_read_day", lambda *_args: (
        {"000001": {}}, {"auction_row_count": 0, "missing_kline_table_count": 0,
                         "kline_rows": {"000001": []}},
    ))
    output = tmp_path / "out"
    main(["--trade-date", "2026-10-14", "--snapshot", str(snapshot),
          "--snapshot-asof", "2026-10-14", "--kline-range", "2026-10-13:2026-10-14",
          "--output-dir", str(output)])
    quality = json.loads((output / "2026-10-14" / "day_quality.json").read_text())
    manifest = json.loads((output / "manifest.json").read_text())
    assert quality["snapshot_asof"] == "2026-10-14"
    assert quality["flags"] == ["NOT_POINT_IN_TIME"]
    assert manifest["NOT_POINT_IN_TIME"] == {"2026-10-14": ["0920", "0924", "0925"]}
