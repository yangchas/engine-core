import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest


SPEC = importlib.util.spec_from_file_location(
    "probe_wencai", Path(__file__).parents[1] / "examples" / "probe_wencai.py"
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class FakeFetcher:
    wencai_cookie = "private-cookie-value-1234567890"

    def __init__(self):
        self.calls = []

    async def _get_wencai_stocks(self, query, *, loop, max_stocks, return_df):
        self.calls.append((query, loop, max_stocks, return_df))
        return pd.DataFrame({"代码": ["000001", "000002"], "名称": ["甲", "乙"]})


def test_run_suite_records_shape_and_sorted_code_hash():
    fetcher = FakeFetcher()
    result = probe.run_suite(
        fetcher, [{"id": "q1", "query": "涨停", "loop": True, "max_stocks": 10, "purpose": "test"}],
        sleep_s=0,
    )
    row = result["results"][0]
    assert row["ok"] is True
    assert row["row_count"] == 2
    assert row["columns"] == ["代码", "名称"]
    assert row["code_column_present"] is True
    assert row["result_hash"] == probe.hash_codes(["000001", "000002"])
    assert fetcher.calls == [("涨停", True, 10, True)]


def test_exception_records_type_without_message():
    class Broken(FakeFetcher):
        async def _get_wencai_stocks(self, *args, **kwargs):
            raise RuntimeError("do not expose this message")

    result = probe.run_suite(
        Broken(), [{"id": "bad", "query": "x", "loop": False, "max_stocks": 1}], sleep_s=0
    )
    row = result["results"][0]
    assert row["ok"] is False
    assert row["error_type"] == "RuntimeError"
    assert "do not expose" not in json.dumps(row)


def test_echo_sanitization_discards_rows_matching_cookie_substring():
    frame = pd.DataFrame({"代码": ["000001", "000002"], "备注": ["安全", "cookie-value-1234567890"]})
    cleaned, redacted = probe.sanitize_frame(frame, FakeFetcher.wencai_cookie)
    assert redacted == 1
    assert cleaned["代码"].tolist() == ["000001"]


def test_five_consecutive_failures_abort_remaining_queries():
    class Broken(FakeFetcher):
        async def _get_wencai_stocks(self, *args, **kwargs):
            raise ValueError("secret message")

    queries = [
        {"id": f"q{i}", "query": "x", "loop": False, "max_stocks": 1}
        for i in range(7)
    ]
    result = probe.run_suite(Broken(), queries, sleep_s=0)
    assert len(result["results"]) == 5
    assert result["manifest"]["aborted"] is True


def test_output_directory_must_be_empty(tmp_path):
    (tmp_path / "existing").write_text("x")
    with pytest.raises(ValueError, match="empty"):
        probe.validate_output_dir(tmp_path)
