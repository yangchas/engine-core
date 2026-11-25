from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from examples.audit_task008_anchor_candidate_alignment import (
    compare_td_anchor_rows_to_q2frame_candidates,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _epoch_ms(clock: str) -> int:
    value = datetime.fromisoformat("2026-09-29T" + clock).replace(tzinfo=SHANGHAI)
    return int(value.timestamp() * 1000)


def _td_row(tag: str, symbol: str, clock: str, price: int | None) -> dict:
    return {
        "auction_tag": tag,
        "trade_date": "20260929",
        "symbol": symbol,
        "ts": "2026-09-29T" + clock,
        "px_milli": price,
    }


def _frame(seq_no: int, updates: list[dict]) -> dict:
    return {
        "version": "Q2FrameV1",
        "seq_no": seq_no,
        "logical_ts_ms": updates[0]["ts"] if updates else _epoch_ms("09:15:00"),
        "phase": 1,
        "q2_updates": updates,
    }


def _update(symbol: str, clock: str, **fields: int) -> dict:
    return {"symbol": symbol, "ts": _epoch_ms(clock), **fields}


def test_alignment_truncates_both_sources_to_second_and_preserves_artifact_order():
    result = compare_td_anchor_rows_to_q2frame_candidates(
        [_td_row("0920", "600000", "09:20:03.100000", 10_500)],
        [
            _frame(
                1,
                [
                    _update("600000", "09:20:03.900000", a20=10_000),
                    # Same logical second; later artifact position wins even
                    # though source milliseconds are smaller.
                    _update("600000", "09:20:03.100000", a20=10_500),
                    _update("600000", "09:20:21.000000", a20=11_000),
                ],
            )
        ],
        trade_date="2026-09-29",
    )

    tag = result["tags"]["0920"]
    assert tag["td_non_null_price_count"] == 1
    assert tag["same_second_price_match_count"] == 1
    assert tag["same_second_price_mismatch_count"] == 0
    assert tag["td_price_seen_anywhere_in_window_count"] == 1
    assert tag["symbol_evidence"][0]["candidate_values_in_window"] == [10_000, 10_500]
    assert tag["td_price_seen_in_snapshot_second_count"] == 1
    assert tag["symbol_evidence"][0]["td_price_candidate_event_seconds_ms"] == [
        _epoch_ms("09:20:03.000000")
    ]
    assert tag["window_end_candidate_match_count"] == 1


def test_missing_td_price_is_reported_as_cohort_difference_not_a_failure():
    result = compare_td_anchor_rows_to_q2frame_candidates(
        [_td_row("0925", "000001", "09:25:06.154000", None)],
        [
            _frame(
                1,
                [
                    _update("000001", "09:25:06.000000", a25=11_350),
                    _update("000001", "09:25:21.000000", a25=12_000),
                ],
            )
        ],
        trade_date="2026-09-29",
    )

    tag = result["tags"]["0925"]
    assert tag["td_null_price_count"] == 1
    assert tag["q2_positive_candidate_when_td_price_null_count"] == 1
    assert tag["diagnostic_status"] == "OBSERVED_DIFFERENCE_NOT_A_GATE"


def test_q2_updates_outside_anchor_window_do_not_become_candidates():
    result = compare_td_anchor_rows_to_q2frame_candidates(
        [_td_row("0924", "600000", "09:24:10.250000", 10_500)],
        [
            _frame(
                1,
                [
                    _update("600000", "09:24:21.000000", a24=10_500),
                ],
            )
        ],
        trade_date="2026-09-29",
    )

    tag = result["tags"]["0924"]
    assert tag["q2_candidate_update_count"] == 0
    assert tag["td_price_seen_anywhere_in_window_count"] == 0
    assert tag["same_second_price_unavailable_count"] == 1
