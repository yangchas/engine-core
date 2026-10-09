from datetime import datetime
from zoneinfo import ZoneInfo

from examples.audit_task008_anchor_q2_field_alignment import (
    compare_td_anchor_fields_to_q2frame,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _epoch_ms(clock: str) -> int:
    value = datetime.fromisoformat("2026-09-30T" + clock).replace(tzinfo=SHANGHAI)
    return int(value.timestamp() * 1000)


def _td_row(
    tag: str,
    symbol: str,
    clock: str,
    *,
    amount: int | None,
    bid: int | None,
    ask: int | None,
) -> dict:
    return {
        "auction_tag": tag,
        "trade_date": "20260930",
        "symbol": symbol,
        "ts": "2026-09-30T" + clock,
        "match_amt_yuan": amount,
        "rest_bid_amt_yuan": bid,
        "rest_ask_amt_yuan": ask,
    }


def _frame(seq_no: int, updates: list[dict]) -> dict:
    return {"version": "Q2FrameV1", "seq_no": seq_no, "q2_updates": updates}


def _update(symbol: str, clock: str, **fields: int | None) -> dict:
    return {"symbol": symbol, "ts": _epoch_ms(clock), **fields}


def test_alignment_truncates_subseconds_and_keeps_zero_distinct_from_null():
    summary, rows = compare_td_anchor_fields_to_q2frame(
        [
            _td_row(
                "0920",
                "600000",
                "09:20:03.276000",
                amount=0,
                bid=None,
                ask=10,
            )
        ],
        [
            _frame(
                1,
                [
                    _update("600000", "09:20:03.900000", am=0, br=0, ar=10),
                    # Whole-second equality intentionally makes the later
                    # artifact entry win even though its source millisecond is
                    # earlier within that second.
                    _update("600000", "09:20:03.100000", am=0, br=5, ar=12),
                    _update("600000", "09:20:04.000000", am=9, br=9, ar=9),
                ],
            )
        ],
        trade_date="2026-09-30",
    )

    assert summary["status"] == "OBSERVED_DIAGNOSTIC_NOT_A_GATE"
    fields = summary["tags"]["0920"]["fields"]
    assert fields["am"]["equal"] == 1
    assert fields["br"]["td_null"] == 1
    assert fields["ar"]["different"] == 1
    assert fields["am"]["by_source_time_gap_seconds"]["0"]["equal"] == 1
    assert fields["ar"]["by_source_time_gap_seconds"]["0"]["different"] == 1
    assert rows[0]["source_time_gap_seconds"] == 0
    assert rows[0]["fields"]["am"]["td_value"] == 0
    assert rows[0]["fields"]["br"]["q2_value"] == 5
    assert rows[0]["fields"]["ar"]["comparison"] == "DIFFERENT"
    assert summary["tags"]["0920"]["td_source_timestamp_range_ms"] == {
        "min": _epoch_ms("09:20:03.276000"),
        "max": _epoch_ms("09:20:03.276000"),
        "unique_timestamp_count": 1,
        "unique_source_second_count": 1,
    }


def test_future_q2_source_update_is_not_selected_for_prior_td_cutoff():
    summary, rows = compare_td_anchor_fields_to_q2frame(
        [
            _td_row(
                "0924",
                "000001",
                "09:24:10.014000",
                amount=100,
                bid=20,
                ask=30,
            )
        ],
        [
            _frame(
                1,
                [
                    _update("000001", "09:14:59.999000", am=100, br=20, ar=30),
                    _update("000001", "09:24:11.000000", am=100, br=20, ar=30),
                ],
            )
        ],
        trade_date="20260930",
    )

    assert summary["tags"]["0924"]["q2_source_time_candidate_rows"] == 0
    assert summary["tags"]["0924"]["fields"]["am"]["no_q2_candidate"] == 1
    assert rows[0]["q2_candidate_source_timestamp_ms"] is None
    assert rows[0]["fields"]["am"]["comparison"] == "NO_Q2_CANDIDATE"


def test_tag_cutoff_summary_counts_distinct_times_without_rounding_away_source_ms():
    summary, _ = compare_td_anchor_fields_to_q2frame(
        [
            _td_row("0920", "600000", "09:20:03.276000", amount=1, bid=2, ask=3),
            _td_row("0920", "600001", "09:20:04.001000", amount=4, bid=5, ask=6),
        ],
        [
            _frame(
                1,
                [
                    _update("600000", "09:20:03.000000", am=1, br=2, ar=3),
                    _update("600001", "09:20:04.000000", am=4, br=5, ar=6),
                ],
            )
        ],
        trade_date="20260930",
    )

    time_range = summary["tags"]["0920"]["td_source_timestamp_range_ms"]
    assert time_range["min"] == _epoch_ms("09:20:03.276000")
    assert time_range["max"] == _epoch_ms("09:20:04.001000")
    assert time_range["unique_timestamp_count"] == 2
    assert time_range["unique_source_second_count"] == 2


def test_all_three_fields_are_counted_independently_and_missing_is_not_zero():
    summary, rows = compare_td_anchor_fields_to_q2frame(
        [
            _td_row(
                "0925",
                "600519",
                "09:25:06.154000",
                amount=100,
                bid=0,
                ask=50,
            )
        ],
        [
            _frame(
                1,
                [_update("600519", "09:25:06.000000", am=100, br=0, ar=None)],
            )
        ],
        trade_date="20260930",
    )

    fields = summary["tags"]["0925"]["fields"]
    assert fields["am"]["equal"] == 1
    assert fields["br"]["equal"] == 1
    assert fields["ar"]["q2_null"] == 1
    assert rows[0]["fields"]["br"]["td_value"] == 0
    assert rows[0]["fields"]["ar"]["q2_value"] is None
    assert rows[0]["fields"]["ar"]["comparison"] == "Q2_NULL"


def test_aggregate_totals_use_only_comparable_values_and_keep_signed_delta():
    summary, _ = compare_td_anchor_fields_to_q2frame(
        [
            _td_row("0920", "600000", "09:20:03", amount=0, bid=0, ask=5),
            _td_row("0920", "600001", "09:20:03", amount=100, bid=20, ask=30),
        ],
        [
            _frame(
                1,
                [
                    _update("600000", "09:20:03", am=0, br=0, ar=5),
                    _update("600001", "09:20:03", am=70, br=25, ar=None),
                ],
            )
        ],
        trade_date="2026-09-30",
    )

    fields = summary["tags"]["0920"]["fields"]
    assert fields["am"]["compared_count"] == 2
    assert fields["am"]["td_value_sum"] == 100
    assert fields["am"]["q2_value_sum"] == 70
    assert fields["am"]["signed_difference_sum"] == -30
    assert fields["am"]["absolute_difference_sum"] == 30
    assert fields["br"]["compared_count"] == 2
    assert fields["br"]["td_value_sum"] == 20
    assert fields["br"]["q2_value_sum"] == 25
    assert fields["br"]["signed_difference_sum"] == 5
    assert fields["ar"]["compared_count"] == 1
    assert fields["ar"]["td_value_sum"] == 5
    assert fields["ar"]["q2_value_sum"] == 5
    assert fields["ar"]["signed_difference_sum"] == 0
    assert fields["ar"]["q2_null"] == 1
