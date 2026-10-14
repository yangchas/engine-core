import hashlib
import pytest

from engine_core.daily_kline_capture import (
    fetch_day,
    load_capture,
    parse_row,
    to_baostock_code,
    validate,
    write_capture,
)


def test_code_mapping_and_row_nulls_are_preserved():
    assert to_baostock_code("688001") == "sh.688001"
    assert to_baostock_code("300001") == "sz.300001"
    assert to_baostock_code("920001") == "bj.920001"
    with pytest.raises(ValueError):
        to_baostock_code("ABC")

    row = parse_row({"code": "sh.600000", "date": "2026-10-14", "open": "", "volume": "0", "amount": ""})
    assert row["symbol"] == "600000"
    assert row["open"] is None and row["volume"] == 0.0 and row["amount_yuan"] is None
    assert row["suspended"] is True


def test_fetch_continues_after_symbol_error_and_stops_after_error_limit():
    class Client:
        def login(self):
            return None

        def query(self, code, date):
            if code.endswith("000002"):
                raise RuntimeError("source error")
            return [{"code": code, "date": date, "close": "10", "preclose": "9", "volume": "1", "amount": "100"}]

        def logout(self):
            return None

    rows, report = fetch_day(Client(), ["000001", "000002", "000003"], "2026-10-14", max_errors=0)
    assert [row["symbol"] for row in rows] == ["000001"]
    assert report["partial"] is True
    assert report["errors"][0]["symbol"] == "000002"


def test_validate_reports_previous_close_and_percent_mismatches():
    row = parse_row({"code": "600000", "date": "2026-10-14", "close": "11", "preclose": "10", "volume": "1", "amount": "100", "pctChg": "2"})
    report = validate([row], prev_close_by_symbol={"600000": 9.0})
    assert report["mismatches"] == [{"symbol": "600000", "field": "pre_close", "actual": 10.0, "expected": 9.0}]
    assert report["pct_chg_mismatches"] == ["600000"]


def test_capture_is_exclusive_and_loader_checks_digest(tmp_path):
    row = parse_row({"code": "600000", "date": "2026-10-14", "close": "10", "preclose": "9", "volume": "1", "amount": "100"})
    output = tmp_path / "capture"
    output.mkdir()
    manifest = write_capture(output, [row], {"date": "2026-10-14", "valid": 1, "suspended": 0, "missing": 0, "error_count": 0, "mismatch_count": 0, "partial": False})
    payload = (output / "daily_kline.jsonl").read_bytes()
    assert manifest["sha256"] == hashlib.sha256(payload).hexdigest()
    assert load_capture(output)["600000"] == row
    with pytest.raises(FileExistsError):
        write_capture(output, [row], {})
    (output / "daily_kline.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sha256"):
        load_capture(output)
