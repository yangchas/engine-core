import hashlib
import json

import pytest

from examples.fetch_kaipan_snapshot import fetch_all


class FakeAnalyzer:
    def __init__(self, pools=None, reasons=None, plates=None, error=None):
        self.pools = pools or {}
        self.reasons = reasons or {}
        self.plates = plates or {}
        self.error = error
        self.calls = []

    def _call_api(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if self.error:
            raise self.error
        return self.pools.get((kwargs["date"], kwargs["ban"]), [])

    def get_ban_reasons(self, symbol):
        self.calls.append(("reason", symbol))
        if self.error:
            raise self.error
        return self.reasons.get(symbol, [])

    def get_his_plates(self, trade_date):
        self.calls.append(("hot_plates", trade_date))
        if self.error:
            raise self.error
        return self.plates.get(trade_date, [])


def test_fetch_writes_raw_hot_plates_and_manifest_count(tmp_path):
    raw = {"List": [{"name": "芯片"}, {"name": "通信"}]}
    analyzer = FakeAnalyzer(plates={"2026-10-13": raw})
    out = tmp_path / "out"
    manifest = fetch_all(analyzer, [], output_dir=out,
                         hot_plate_dates=["2026-10-13"], sleep_s=0,
                         sleep_fn=lambda _: None)
    assert json.loads((out / "hot_plates/2026-10-13.json").read_text()) == raw
    assert manifest["hot_plate_counts"] == {"2026-10-13": 2}
    assert analyzer.calls == [("hot_plates", "2026-10-13")]


def test_empty_hot_plate_response_counts_failure_and_writes_no_file(tmp_path):
    analyzer = FakeAnalyzer(plates={"2026-10-13": None})
    out = tmp_path / "out"
    manifest = fetch_all(analyzer, [], output_dir=out,
                         hot_plate_dates=["2026-10-13"], sleep_s=0,
                         sleep_fn=lambda _: None)
    assert manifest["failures"] == [{"error_type": "EmptyResponse", "date": "2026-10-13"}]
    assert manifest["hot_plate_counts"] == {"2026-10-13": None}
    assert not (out / "hot_plates/2026-10-13.json").exists()


def test_cli_allows_empty_pool_dates_for_hot_plates_only(tmp_path, monkeypatch):
    import sys
    import types

    analyzer = FakeAnalyzer(plates={"2026-10-13": [{"name": "芯片"}]})
    package = types.ModuleType("ai")
    api = types.ModuleType("ai.API")
    stock_analyzer = types.ModuleType("ai.API.StockAnalyzer")
    stock_analyzer.StockAnalyzer = lambda: analyzer
    monkeypatch.setitem(sys.modules, "ai", package)
    monkeypatch.setitem(sys.modules, "ai.API", api)
    monkeypatch.setitem(sys.modules, "ai.API.StockAnalyzer", stock_analyzer)

    from examples.fetch_kaipan_snapshot import main
    assert main(["--legacy-root", str(tmp_path), "--output-dir", str(tmp_path / "out"),
                 "--dates", "", "--hot-plate-dates", "2026-10-13",
                 "--sleep-s", "0"]) == 0
    assert analyzer.calls == [("hot_plates", "2026-10-13")]


def test_fetch_writes_raw_pool_reason_and_manifest_hashes(tmp_path):
    row = ["600001", "测试", *([None] * 14)]
    pool = {"info": [[row], ["2026-10-09"]], "ttag": "x", "errcode": "0"}
    analyzer = FakeAnalyzer({("2026-10-09", "1"): pool}, {"600001": {"raw": ["原因"]}})
    out = tmp_path / "snapshot"
    manifest = fetch_all(analyzer, ["2026-10-09"], output_dir=out, max_ban=1,
                         sleep_s=0, sleep_fn=lambda _: None)
    assert json.loads((out / "pools/2026-10-09_ban1.json").read_text()) == pool
    assert json.loads((out / "reasons/600001.json").read_text()) == {"raw": ["原因"]}
    assert [call for call in analyzer.calls if call[0] == "reason"] == [("reason", "600001")]
    assert manifest["pool_counts"] == {"2026-10-09": {"1": 1}}
    assert manifest["pool_symbol_counts"] == {"2026-10-09": 1}
    assert manifest["request_count"] == 2
    for name, digest in manifest["sha256"].items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    assert set(path.relative_to(out).as_posix() for path in out.rglob("*") if path.is_file()) == {
        "pools/2026-10-09_ban1.json", "reasons/600001.json", "manifest.json"
    }


def test_pool_parses_records_ignoring_metadata_and_empty_pages_and_dedupes_symbols(tmp_path):
    row_a = ["SH600001", "甲", *([None] * 14)]
    row_b = ["000002", "乙", *([None] * 14)]
    pool_one = {"info": [[row_a, row_b], ["2026-10-09", "meta"]], "ttag": "x", "errcode": "0"}
    pool_two = {"info": [[], ["2026-10-09"]], "ttag": "x", "errcode": "0"}
    analyzer = FakeAnalyzer({("2026-10-09", "1"): pool_one,
                             ("2026-10-09", "2"): pool_two},
                            {"600001": {}, "000002": {}})
    manifest = fetch_all(analyzer, ["2026-10-09"], output_dir=tmp_path / "out", max_ban=2,
                         sleep_s=0, sleep_fn=lambda _: None)
    assert manifest["pool_counts"] == {"2026-10-09": {"1": 2, "2": 0}}
    assert manifest["pool_symbol_counts"] == {"2026-10-09": 2}
    assert [call for call in analyzer.calls if call[0] == "reason"] == [
        ("reason", "600001"), ("reason", "000002")
    ]


@pytest.mark.parametrize("response", [None, [], {"other": []}, {"info": None}])
def test_unexpected_pool_shapes_are_counted_without_raising(tmp_path, response):
    analyzer = FakeAnalyzer({("2026-10-09", "1"): response})
    manifest = fetch_all(analyzer, ["2026-10-09"], output_dir=tmp_path / "out", max_ban=1,
                         sleep_s=0, sleep_fn=lambda _: None)
    assert manifest["failures"] == [{"error_type": "UnexpectedShape", "date": "2026-10-09", "lvl": 1}]


def test_metadata_only_pool_page_is_unexpected_and_keeps_failure_streak(tmp_path):
    analyzer = FakeAnalyzer({
        ("2026-10-09", str(level)): {"info": ["metadata"]}
        for level in range(1, 9)
    })
    manifest = fetch_all(analyzer, ["2026-10-09"], output_dir=tmp_path / "out", max_ban=8,
                         sleep_s=0, sleep_fn=lambda _: None)
    assert manifest["request_count"] == 8
    assert manifest["aborted"] is True
    assert [failure["error_type"] for failure in manifest["failures"]] == ["UnexpectedShape"] * 8


def test_max_requests_truncates_before_next_request(tmp_path):
    analyzer = FakeAnalyzer({("2026-10-09", "1"): [{"code": "600001"}]})
    manifest = fetch_all(analyzer, ["2026-10-09"], output_dir=tmp_path / "out",
                         max_ban=3, max_requests=1, sleep_s=0, sleep_fn=lambda _: None)
    assert manifest["request_count"] == 1
    assert manifest["truncated"] is True
    assert len(analyzer.calls) == 1


def test_eight_consecutive_empty_responses_abort(tmp_path):
    analyzer = FakeAnalyzer()
    manifest = fetch_all(analyzer, ["2026-10-09"], output_dir=tmp_path / "out",
                         max_ban=12, sleep_s=0, sleep_fn=lambda _: None)
    assert manifest["request_count"] == 8
    assert manifest["aborted"] is True
    assert len(manifest["failures"]) == 8


def test_nonempty_output_directory_is_rejected(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "existing").write_text("keep")
    with pytest.raises(FileExistsError):
        fetch_all(FakeAnalyzer(), ["2026-10-09"], output_dir=out,
                  sleep_s=0, sleep_fn=lambda _: None)
    assert (out / "existing").read_text() == "keep"


def test_exception_messages_and_sensitive_terms_are_not_serialized(tmp_path):
    analyzer = FakeAnalyzer(error=RuntimeError("Token=secret DeviceID=private"))
    out = tmp_path / "out"
    manifest = fetch_all(analyzer, ["2026-10-09"], output_dir=out, max_ban=1,
                         sleep_s=0, sleep_fn=lambda _: None)
    serialized = (out / "manifest.json").read_text()
    assert manifest["failures"] == [{"error_type": "RuntimeError", "date": "2026-10-09", "lvl": 1}]
    assert "secret" not in serialized and "private" not in serialized
    assert "Token" not in serialized and "DeviceID" not in serialized


def test_injected_reason_fetcher_uses_page_size_and_writes_raw_response(tmp_path):
    pool = {"info": [[ ["600001", "甲", *([None] * 14)] ]]}
    raw_reason = {"StockID": "600001", "List": [{"Date": "2026-10-12", "Reason": "原始"}]}
    calls = []
    analyzer = FakeAnalyzer({("2026-10-09", "1"): pool})

    def fetcher(code, page_size):
        calls.append((code, page_size))
        return raw_reason

    out = tmp_path / "out"
    fetch_all(analyzer, ["2026-10-09"], output_dir=out, max_ban=1,
              reason_page_size=40, reason_fetcher=fetcher,
              sleep_s=0, sleep_fn=lambda _: None)
    assert calls == [("600001", 40)]
    assert json.loads((out / "reasons/600001.json").read_text()) == raw_reason
    assert not [call for call in analyzer.calls if call[0] == "reason"]


def test_response_echoing_runtime_credentials_is_redacted_before_write(tmp_path):
    config = {"Token": "test-token-value", "DeviceID": "test-device-value"}
    response = {"info": [[ ["600001", "甲", *([None] * 14)] ]],
                "echo": {"token": config["Token"], "nested": [config["DeviceID"]]}}
    analyzer = FakeAnalyzer({("2026-10-09", "1"): response})
    out = tmp_path / "out"
    manifest = fetch_all(analyzer, ["2026-10-09"], output_dir=out, max_ban=1,
                         credential_config=config, sleep_s=0, sleep_fn=lambda _: None)
    serialized = (out / "pools/2026-10-09_ban1.json").read_text()
    assert "test-token-value" not in serialized and "test-device-value" not in serialized
    assert manifest["redacted_response_count"] == 2
    assert json.loads(serialized)["echo"] == {"token": "<redacted>", "nested": ["<redacted>"]}


def test_reason_response_equal_to_page_size_is_marked_possibly_truncated(tmp_path):
    raw_reason = {"List": [{"Date": "2026-10-12"}, {"Date": "2026-10-11"}]}
    analyzer = FakeAnalyzer({("2026-10-09", "1"): {"info": [[
        ["600001", "甲", *([None] * 14)]
    ]]}})
    manifest = fetch_all(analyzer, ["2026-10-09"], output_dir=tmp_path / "out",
                         max_ban=1, reason_page_size=2,
                         reason_fetcher=lambda _code, _page_size: raw_reason,
                         sleep_s=0, sleep_fn=lambda _: None)
    assert manifest["possibly_truncated_symbols"] == ["600001"]


def test_manifest_is_incrementally_written_at_configured_interval(tmp_path):
    pool_rows = [[f"60000{i}", f"甲{i}", *([None] * 14)] for i in range(1, 4)]
    analyzer = FakeAnalyzer({("2026-10-09", "1"): {"info": [pool_rows]}})
    out = tmp_path / "out"
    observed_request_counts = []

    def fetcher(code, _page_size):
        manifest_path = out / "manifest.json"
        observed_request_counts.append(
            json.loads(manifest_path.read_text())["request_count"] if manifest_path.exists() else None
        )
        return {"List": [{"Date": "2026-10-12"}]}

    fetch_all(analyzer, ["2026-10-09"], output_dir=out, max_ban=1,
              reason_fetcher=fetcher, manifest_every=2,
              sleep_s=0, sleep_fn=lambda _: None)
    assert observed_request_counts == [None, 2, 2]
    assert json.loads((out / "manifest.json").read_text())["request_count"] == 4
