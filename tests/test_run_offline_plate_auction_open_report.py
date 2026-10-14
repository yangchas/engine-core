import csv
import json

from examples.run_offline_plate_auction_open_report import (
    _attach_independent_plates,
    _load_registry_dir,
    _registry_point_in_time_flag,
)


def test_registry_loader_accepts_list_registry_and_reports_asof_flag(tmp_path):
    (tmp_path / "registry.json").write_text(json.dumps([
        {"code": "801001", "name": "AI应用", "class": "THEME"},
    ]), encoding="utf-8")
    (tmp_path / "relation.csv").write_text(
        "plateid,stockid\n801001,000001\n", encoding="utf-8"
    )

    registry, members = _load_registry_dir(tmp_path)

    assert registry["801001"]["name"] == "AI应用"
    assert members == {"801001": {"000001"}}
    assert _registry_point_in_time_flag("2026-10-13", "2026-10-13") is True
    assert _registry_point_in_time_flag("2026-10-12", "2026-10-13") is False
    (tmp_path / "registry.json").write_text(
        json.dumps({"801002": {"name": "通信", "class": "THEME"}}), encoding="utf-8"
    )
    keyed_registry, _ = _load_registry_dir(tmp_path)
    assert keyed_registry["801002"]["name"] == "通信"


def test_independent_report_keeps_missing_returns_missing(tmp_path):
    rows = [{"code": "801001", "name": "AI应用", "class": "THEME"}]
    (tmp_path / "registry.json").write_text(json.dumps({"plates": rows}), encoding="utf-8")
    with (tmp_path / "relation.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("plateid", "stockid"))
        writer.writerows(("801001", f"00000{i}") for i in range(1, 10))

    stocks = []
    for i in range(1, 10):
        value = None if i == 9 else (i - 5) * 0.01
        stocks.append({
            "symbol": f"00000{i}",
            "times": {tag: {"pct_vs_pre_close": value, "limit_state": None}
                      for tag in ("0920", "0924", "0925", "0932")},
        })
    report = {"stocks": stocks, "plates": [{"plate": "AI应用", "times": {
        tag: {"hot_live_rank": None, "hot_prev_close_rank": None}
        for tag in ("0920", "0924", "0925", "0932")
    }}]}

    _attach_independent_plates(report, tmp_path, "2026-10-13", "2026-10-13")

    row = report["plates"][0]["times"]["0925"]["independent"]
    assert row["market"]["n"] == 8
    assert row["missing_count"] == 1
    assert row["uncovered"]["n"] <= 8
    assert row["flags"] == ["NOT_POINT_IN_TIME"]
