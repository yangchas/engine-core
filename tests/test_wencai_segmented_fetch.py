import importlib.util
from pathlib import Path

import pandas as pd


SPEC = importlib.util.spec_from_file_location(
    "wencai_segmented_fetch",
    Path(__file__).parents[1] / "examples" / "wencai_segmented_fetch.py",
)
segmented = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(segmented)


def test_capped_segment_splits_and_rows_are_deduplicated():
    calls = []

    def fetch(query, loop, max_stocks):
        calls.append(query)
        if "小于20亿" in query and "大于" not in query:
            return pd.DataFrame({"股票代码": ["000001", "000002", "000003"]})
        if "大于10亿" in query and "小于20亿" in query:
            return pd.DataFrame({"股票代码": ["000002"]})
        if "大于20亿" in query:
            return pd.DataFrame({"股票代码": ["000003"]})
        return pd.DataFrame({"股票代码": ["000001", "000002"]})

    result = segmented.fetch_segmented(
        fetch, "基础查询", edges_yi=(0, 20, None), cap=4, sleep_s=0,
    )

    assert len(calls) > 2
    assert [row["股票代码"] for row in result.rows] == ["000001", "000002", "000003"]
    assert result.duplicate_count == 1
    assert result.boundary_duplicates == 1
    assert result.complete is True


def test_open_ended_capped_range_is_unresolved():
    result = segmented.fetch_segmented(
        lambda query, loop, cap: pd.DataFrame({"股票代码": [str(i) for i in range(999)]}),
        "基础", edges_yi=(0, None), cap=1000, sleep_s=0,
    )
    assert result.segments[0]["capped"] is True
    assert result.complete is False
    assert "capped_unresolved" in result.uncapped_cover_note


def test_failed_segment_retries_once_and_records_type():
    calls = []

    def fail(query, loop, cap):
        calls.append(query)
        raise RuntimeError("private details")

    result = segmented.fetch_segmented(
        fail, "基础", edges_yi=(0, None), max_queries=2, sleep_s=0,
    )
    assert len(calls) == 2
    assert result.segments[0]["ok"] is False
    assert result.segments[0]["error_type"] == "RuntimeError"
    assert result.complete is False
    assert "private details" not in str(result.segments)


def test_query_limit_marks_remaining_ranges_incomplete():
    calls = []

    def fetch(query, loop, cap):
        calls.append(query)
        return pd.DataFrame({"股票代码": [f"{len(calls):06d}"]})

    result = segmented.fetch_segmented(
        fetch, "基础", edges_yi=(0, 10, 20, None), max_queries=1, sleep_s=0,
    )
    assert len(calls) == 1
    assert result.complete is False
    assert "query_limit_reached=1" in result.uncapped_cover_note


def test_output_directory_must_be_empty(tmp_path):
    (tmp_path / "existing").write_text("x")
    try:
        segmented.validate_output_dir(tmp_path)
    except ValueError as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("nonempty output directory should be rejected")

