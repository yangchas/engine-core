from engine_core.f10_refresh import merge_f10, normalize_wencai_rows


def test_normalize_wencai_dates_codes_and_missing_values():
    rows = normalize_wencai_rows([
        {"股票代码[20261013]": "1.SZ", "股票简称[20261013]": "甲", "总市值[20261013]": "--"},
        {"股票代码[20261014]": "000001.SZ", "股票简称[20261014]": "甲新", "总市值[20261014]": "1,234.5"},
    ], "mkt")
    assert rows["000001"]["总市值"] == 1234.5
    assert rows["000001"]["股票简称"] == "甲新"
    assert rows["000001"]["_asof"] == "20261014"
    assert normalize_wencai_rows([{"股票代码": "600000.SH", "总市值": "--"}], "mkt")["600000"]["总市值"] is None


def test_merge_priority_stale_old_added_symbols_and_never_zero_fills():
    old = [
        {"股票代码": "000001", "股票简称": "旧名", "总市值": "100", "a股市值(不含限售股)": "80"},
        {"股票代码": "000002", "股票简称": "旧股", "总市值": "--", "a股市值(不含限售股)": ""},
    ]
    groups = {"mkt": {"000001": {"股票代码": "000001", "总市值": 120.0, "a股市值(不含限售股)": None, "_asof": "20261014"},
                      "000003": {"股票代码": "000003", "股票简称": "新股", "总市值": 9.0, "_asof": "20261014"}}}
    rows, report = merge_f10(old, groups, new_asof="2026-10-14")
    by_code = {r["股票代码"]: r for r in rows}
    assert set(by_code) == {"000001", "000002", "000003"}
    assert by_code["000001"]["总市值"] == 120.0
    assert by_code["000001"]["_source"]["总市值"] == "WENCAI_NEW"
    assert by_code["000001"]["a股市值(不含限售股)"] == 80.0
    assert by_code["000002"]["总市值"] is None
    assert by_code["000002"]["_source"]["总市值"] == "MISSING"
    assert by_code["000002"]["_row_status"] == "STALE_OLD"
    assert by_code["000003"]["股票简称"] == "新股"
    assert report["added_symbols"] == 1
    assert report["retained_symbols"] == 1


def test_caps_are_last_resort_and_unit_conflicts_are_reported():
    old = [{"股票代码": "000001", "股票简称": "甲", "a股市值(不含限售股)": "80"}]
    groups = {"mkt": {"000001": {"股票代码": "000001", "a股市值(不含限售股)": 90.0,
                                    "_asof": "20261014", "_units": {"a股市值(不含限售股)": "yi"}}}}
    rows, report = merge_f10(old, groups, caps={"000001": {"float_cap": 100}}, new_asof="2026-10-14")
    row = rows[0]
    assert row["a股市值(不含限售股)"] == 90.0
    assert row["_source"]["a股市值(不含限售股)"] == "UNIT_CONFLICT"
    assert report["unit_conflicts"][0]["symbol"] == "000001"

