from datetime import datetime, timedelta, timezone

import pytest

from engine_core import (
    MarketStateReducer,
    RedisQ2ProjectionAdapter,
    build_segment_frame,
    compare_adjacent_segments,
    compare_segments,
    compute_resting_order_pressure,
)
from engine_core.contracts import semantic_hash


class FakeRedis:
    def __init__(self, price, amount, bid, ask, timestamp):
        self.active = {"q2:active:2026-09-04": {"000001"}}
        self.hashes = {
            "q2:000001": {
                "px": str(price),
                "pc": "990",
                "amt": str(amount),
                "am": str(amount),
                "vol": "100",
                "br": str(bid),
                "ar": str(ask),
                "ts": str(timestamp),
            }
        }

    def smembers(self, key):
        return self.active.get(key, set())

    def hgetall(self, key):
        return self.hashes.get(key, {})


def _snapshot(price, amount, bid, ask, time_text, trigger):
    from engine_core.windows import local_time_ms

    ts = local_time_ms("2026-09-04", time_text)
    observed = datetime(2026, 9, 4, 1, 30, tzinfo=timezone.utc)
    projection = RedisQ2ProjectionAdapter(
        FakeRedis(price, amount, bid, ask, ts)
    ).read("2026-09-04", observed)
    reducer = MarketStateReducer()
    reducer.apply_snapshot(projection, logical_time_ms=ts, phase="AUCTION")
    return reducer.build_snapshot(trigger, logical_time_ms=ts)


def test_segment_frame_keeps_fact_groups_independently_statused():
    start = _snapshot(1000, 100, 20, 10, "09:19:00", "START")
    end = _snapshot(1020, 200, 30, 10, "09:20:00", "AUCTION_0920")
    frame = build_segment_frame(
        "auction_trial",
        start,
        end,
        scope_type="SYMBOL",
        scope_id="000001",
        amount_semantics="CUMULATIVE",
        volume_semantics="CUMULATIVE",
    )
    assert frame.price.status.value == "READY"
    assert frame.price.return_bp == 200
    assert frame.volume.amount_delta_yuan == 100.0
    assert frame.order_book.directional_pressure_yuan == 20.0
    assert frame.breadth.status.value == "UNAVAILABLE"
    assert frame.theme.status.value == "UNAVAILABLE"
    assert frame.quality.unavailable_groups == ("breadth", "theme")
    assert frame.price.field_lineage["end_price_milli"] == (end.snapshot_id,)


def test_segment_comparison_is_dimensioned_not_a_total_strength_score():
    first_start = _snapshot(1000, 100, 20, 10, "09:19:00", "START1")
    first_end = _snapshot(1020, 200, 30, 10, "09:20:00", "END1")
    second_end = _snapshot(1010, 350, 25, 20, "09:24:00", "END2")
    first = build_segment_frame(
        "auction_trial",
        first_start,
        first_end,
        scope_type="SYMBOL",
        scope_id="000001",
        amount_semantics="CUMULATIVE",
        volume_semantics="CUMULATIVE",
    )
    second = build_segment_frame(
        "auction_reprice",
        first_end,
        second_end,
        scope_type="SYMBOL",
        scope_id="000001",
        amount_semantics="CUMULATIVE",
        volume_semantics="CUMULATIVE",
    )
    comparison = compare_segments(first, second)
    assert comparison.price_change == "PRICE_WEAKER"
    assert comparison.volume_change == "VOLUME_EXPANDING"
    assert comparison.order_book_change == "PRESSURE_WEAKENING"
    assert comparison.breadth_change == "BREADTH_UNAVAILABLE"
    assert comparison.theme_change == "THEME_UNAVAILABLE"
    assert "price.return_bp" in comparison.reason_codes


def test_adjacent_comparison_rejects_non_contiguous_segments():
    first_start = _snapshot(1000, 100, 20, 10, "09:19:00", "START1")
    first_end = _snapshot(1020, 200, 30, 10, "09:20:00", "END1")
    second_end = _snapshot(1010, 350, 25, 20, "09:24:00", "END2")
    first = build_segment_frame(
        "first", first_start, first_end,
        scope_type="SYMBOL", scope_id="000001",
        amount_semantics="CUMULATIVE", volume_semantics="CUMULATIVE",
    )
    non_adjacent = build_segment_frame(
        "non_adjacent", first_start, second_end,
        scope_type="SYMBOL", scope_id="000001",
        amount_semantics="CUMULATIVE", volume_semantics="CUMULATIVE",
    )
    with pytest.raises(ValueError):
        compare_adjacent_segments(first, non_adjacent)


def test_resting_order_pressure_is_a_missing_safe_pure_function():
    assert compute_resting_order_pressure(30, 10) == 20
    assert compute_resting_order_pressure(0, 0) == 0
    assert compute_resting_order_pressure(None, 10) is None


def test_minimal_0920_to_0924_case_is_engine_independent_and_repeatable():
    # The legacy producer fires the A20/A24 anchors at 09:20:03 and 09:24:10;
    # the test keeps those observed times distinct from requested wall labels.
    base = _snapshot(990, 40, 18, 12, "09:19:59", "AUCTION_START")
    start = _snapshot(1000, 100, 20, 10, "09:20:03", "AUCTION_TRIAL_END")
    end = _snapshot(1015, 280, 32, 12, "09:24:10", "AUCTION_REPRICE_END")
    first = build_segment_frame(
        "auction_trial",
        base,
        start,
        scope_type="SYMBOL",
        scope_id="000001",
        amount_semantics="CUMULATIVE",
        volume_semantics="CUMULATIVE",
    )
    second = build_segment_frame(
        "auction_reprice",
        start,
        end,
        scope_type="SYMBOL",
        scope_id="000001",
        amount_semantics="CUMULATIVE",
        volume_semantics="CUMULATIVE",
    )
    comparison = compare_adjacent_segments(first, second)
    assert first.price.return_bp == 101
    assert second.price.return_bp == 150
    assert second.volume.amount_delta_yuan == 180
    assert second.order_book.directional_pressure_yuan == 20
    assert comparison.price_change == "PRICE_STRONGER"
    assert comparison.volume_change == "VOLUME_EXPANDING"
    assert comparison.order_book_change == "PRESSURE_IMPROVING"
    assert semantic_hash(first) == semantic_hash(
        build_segment_frame(
            "auction_trial",
            base,
            start,
            scope_type="SYMBOL",
            scope_id="000001",
            amount_semantics="CUMULATIVE",
            volume_semantics="CUMULATIVE",
        )
    )


def test_missing_symbol_does_not_become_zero_facts():
    start = _snapshot(1000, 100, 20, 10, "09:19:00", "START")
    end = _snapshot(1000, 100, 20, 10, "09:20:00", "END")
    frame = build_segment_frame(
        "missing",
        start,
        end,
        scope_type="SYMBOL",
        scope_id="000002",
    )
    assert frame.quality.status.value == "MISSING"
    assert frame.price.start_price_milli is None
    assert frame.volume.amount_delta_yuan is None


def test_unknown_counter_semantics_does_not_create_fake_delta():
    start = _snapshot(1000, 100, 20, 10, "09:19:00", "START")
    end = _snapshot(1020, 200, 30, 10, "09:20:00", "END")
    frame = build_segment_frame(
        "unknown-semantics",
        start,
        end,
        scope_type="SYMBOL",
        scope_id="000001",
    )
    assert frame.volume.status.value == "UNAVAILABLE"
    assert frame.volume.amount_delta_yuan is None
    assert "volume" in frame.quality.unavailable_groups


def test_non_symbol_scope_is_explicitly_unavailable():
    start = _snapshot(1000, 100, 20, 10, "09:19:00", "START")
    end = _snapshot(1020, 200, 30, 10, "09:20:00", "END")
    frame = build_segment_frame(
        "theme-scope",
        start,
        end,
        scope_type="THEME",
        scope_id="theme-a",
    )
    assert frame.quality.status.value == "UNAVAILABLE"
    assert frame.price.status.value == "UNAVAILABLE"
    assert frame.volume.status.value == "UNAVAILABLE"


def test_non_positive_price_is_invalid_not_missing():
    start = _snapshot(0, 100, 20, 10, "09:19:00", "START")
    end = _snapshot(1020, 200, 30, 10, "09:20:00", "END")
    frame = build_segment_frame(
        "invalid-price",
        start,
        end,
        scope_type="SYMBOL",
        scope_id="000001",
        amount_semantics="CUMULATIVE",
        volume_semantics="CUMULATIVE",
    )
    assert frame.price.status.value == "INVALID"
    assert frame.price.return_bp is None
    assert "price" in frame.quality.missing_fields
