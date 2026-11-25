from engine_core.wencai_fallback import (
    fetch_auction_fallback,
    fetch_limit_up_fallback,
)


def test_auction_queries_dated_wen_cai_and_prefers_non_st_duplicate():
    calls = []

    def fetch(query, loop, max_stocks):
        calls.append(query)
        suffix = "20261013"
        return [
            {"股票代码": "000001", "股票简称": "平安银行", f"竞价金额[{suffix}]": 6_000_000,
             f"竞价匹配价[{suffix}]": 10.2, f"竞价涨幅[{suffix}]": 1.2},
            {"股票代码": "000001", "股票简称": "平安银行ST", f"竞价金额[{suffix}]": 6_000_000,
             f"竞价匹配价[{suffix}]": 10.2, f"竞价涨幅[{suffix}]": 1.2},
        ]

    result = fetch_auction_fallback(fetch, date="2026-10-13", include_st=True, sleep_s=0)
    assert calls == [
        "2026年10月13日集合竞价金额大于500万;去除st",
        "2026年10月13日集合竞价金额大于500万;st",
    ]
    assert len(result.rows) == 1
    assert result.rows[0]["is_st"] is False
    assert result.rows[0]["amount_yuan"] == 6_000_000
    assert result.requested_date == "2026-10-13"


def test_auction_date_mismatch_keeps_rows_and_marks_incomplete():
    result = fetch_auction_fallback(
        lambda *args: [{"股票代码": "000001", "竞价金额[20261012]": 5_100_000}],
        date="2026-10-13", include_st=False,
    )
    assert len(result.rows) == 1
    assert "DATE_MISMATCH" in result.labels
    assert result.complete is False


def test_query_failure_retries_once_then_returns_partial():
    calls = []

    def broken(*args):
        calls.append(args[0])
        raise RuntimeError("private details")

    result = fetch_auction_fallback(
        broken, date="2026-10-13", include_st=False, sleep_s=0,
    )
    assert len(calls) == 2
    assert result.complete is False
    assert "PARTIAL_QUERY_FAILED" in result.labels
    assert "private details" not in str(result.query_log)


def test_query_budget_bounds_include_st_queries():
    calls = []
    result = fetch_auction_fallback(
        lambda query, *args: calls.append(query) or [],
        date="2026-10-13", include_st=True, max_queries=1,
    )
    assert len(calls) == 1
    assert "PARTIAL_QUERY_FAILED" in result.labels


def test_limit_up_fields_and_missing_seal_are_normalized():
    result = fetch_limit_up_fallback(
        lambda *args: [
            {"股票代码": "600000", "股票简称": "浦发银行",
             "连续涨停天数[20261013]": 2, "首次涨停时间[20261013]": "09:30:00",
             "最终涨停时间[20261013]": "14:50:00", "涨停封单额[20261013]": 12345,
             "涨停封单量[20261013]": 100, "涨停开板次数[20261013]": 3,
             "涨停原因类别[20261013]": "金融"},
            {"股票代码": "000001", "连续涨停天数[20261013]": 1},
        ],
        date="2026-10-13", include_st=True,
    )
    sealed = next(row for row in result.rows if row["symbol"] == "600000")
    missing = next(row for row in result.rows if row["symbol"] == "000001")
    assert sealed["lb_days"] == 2
    assert sealed["seal_amount_yuan"] == 12345
    assert sealed["sealed"] is True
    assert missing["seal_amount_yuan"] is None
    assert missing["sealed"] is None
    assert "SEAL_EVIDENCE_FROM_WENCAI" in result.labels
