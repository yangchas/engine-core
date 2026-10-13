from engine_core import market_cap_unit_check, normalize_f10_market_caps
from engine_core.facts import FactStatus


def _row(code, cap="1000000000", float_cap="800000000", shares="100000000", float_shares="80000000", name="n"):
    return {"股票代码": code, "股票简称": name, "总市值": cap, "a股市值(不含限售股)": float_cap,
            "总股本": shares, "流通a股": float_shares}


def test_normalizes_codes_units_and_never_zero_fills():
    caps = normalize_f10_market_caps(
        [_row("000970.SZ"), _row("600000.SH", float_cap=""), _row("bad"), _row("300001.SZ", float_cap="0")],
        as_of="2026-09-14")
    assert set(caps.rows) == {"000970", "600000", "300001"}
    assert caps.circulating_market_cap_yi("000970") == 8.0
    assert caps.rows["600000"]["circulating_market_cap_yuan"] is None
    assert caps.rows["300001"]["circulating_market_cap_yuan"] is None  # 0 is not a value
    assert caps.circulating_market_cap_yi("600000") is None
    assert caps.invalid_symbol_rows == 1
    assert caps.missing_cap_symbols == ("300001", "600000")
    assert caps.status is FactStatus.PARTIAL


def test_status_ready_only_with_as_of_and_complete_rows():
    rows = [_row("000970.SZ"), _row("600000.SH")]
    assert normalize_f10_market_caps(rows, as_of="2026-09-14").status is FactStatus.READY
    assert normalize_f10_market_caps(rows).status is FactStatus.PARTIAL  # as-of unknown
    assert normalize_f10_market_caps([]).status is FactStatus.UNAVAILABLE


def test_duplicate_codes_are_rejected_in_every_occurrence():
    caps = normalize_f10_market_caps([_row("000970.SZ"), _row("000970.SZ", cap="5")], as_of="d")
    assert "000970" not in caps.rows and caps.duplicate_symbols == ("000970",)
    assert caps.status is FactStatus.UNAVAILABLE


def test_unit_check_detects_wan_yuan_columns():
    yuan = normalize_f10_market_caps([_row("000970.SZ")], as_of="d")
    assert market_cap_unit_check(yuan, {"000970": 10.0}) == {
        "count": 1, "median": 1.0, "units_consistent": True}
    wan = normalize_f10_market_caps([_row("000970.SZ", cap="100000")], as_of="d")  # 10k-yuan column
    check = market_cap_unit_check(wan, {"000970": 10.0})
    assert check["units_consistent"] is False
