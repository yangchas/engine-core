from datetime import datetime, timezone

import pytest

from engine_core import MinuteWindowTracker, minute_key_from_timestamp_ms


def _ms(text: str) -> int:
    value = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def test_minute_key_uses_explicit_shanghai_timezone():
    trade_date, minute = minute_key_from_timestamp_ms(_ms("2026-09-04T01:20:03+00:00"))
    assert trade_date == "2026-09-04"
    assert minute == 9 * 60 + 20


def test_tracker_derives_one_minute_price_and_exact_two_minute_amount():
    tracker = MinuteWindowTracker(keep_minutes=3)
    tracker.observe("600519", price_milli=1000, amount_yuan=100, source_time_ms=_ms("2026-09-04T01:18:01+00:00"))
    tracker.observe("600519", price_milli=1010, amount_yuan=220, source_time_ms=_ms("2026-09-04T01:19:04+00:00"))
    metrics = tracker.observe("600519", price_milli=1020, amount_yuan=350, source_time_ms=_ms("2026-09-04T01:20:02+00:00"))
    assert metrics.price_change_bp == 99
    assert metrics.price_change_reason == "READY"
    assert metrics.amount_2m_yuan == 250
    assert metrics.amount_2m_reason == "READY"


def test_tracker_requires_adjacent_price_but_uses_legacy_amount_lookback():
    tracker = MinuteWindowTracker()
    tracker.observe("600519", price_milli=1000, amount_yuan=100, source_time_ms=_ms("2026-09-04T01:18:00+00:00"))
    missing = tracker.observe("600519", price_milli=1020, amount_yuan=350, source_time_ms=_ms("2026-09-04T01:20:00+00:00"))
    assert missing.price_change_bp is None
    assert missing.amount_2m_yuan == 250
    assert missing.amount_2m_reason == "READY"

    tracker.observe("600519", price_milli=1030, amount_yuan=500, source_time_ms=_ms("2026-09-04T01:19:00+00:00"))
    reset = tracker.observe("600519", price_milli=1040, amount_yuan=50, source_time_ms=_ms("2026-09-04T01:20:30+00:00"))
    assert reset.amount_2m_yuan is None
    assert reset.amount_2m_reason == "COUNTER_RESET"


def test_tracker_duplicate_timestamp_is_idempotent_but_conflict_rejected():
    tracker = MinuteWindowTracker()
    timestamp = _ms("2026-09-04T01:20:00+00:00")
    first = tracker.observe("600519", price_milli=1000, amount_yuan=100, source_time_ms=timestamp)
    repeat = tracker.observe("600519", price_milli=1000, amount_yuan=100, source_time_ms=timestamp)
    assert repeat.content_hash == first.content_hash
    with pytest.raises(ValueError, match="conflicting observations"):
        tracker.observe("600519", price_milli=1001, amount_yuan=100, source_time_ms=timestamp)


def test_tracker_has_no_wall_clock_fallback_and_keeps_days_separate():
    tracker = MinuteWindowTracker(keep_minutes=2)
    with pytest.raises(ValueError):
        tracker.observe("600519", price_milli=1000, amount_yuan=1, source_time_ms=0)
    tracker.observe("600519", price_milli=1000, amount_yuan=100, source_time_ms=_ms("2026-09-04T01:20:00+00:00"))
    tracker.observe("600519", price_milli=1100, amount_yuan=200, source_time_ms=_ms("2026-09-05T01:20:00+00:00"))
    assert set(tracker.get_all_metrics(trade_date="2026-09-04")) == {"600519"}
    assert tracker.get_metrics("600519", trade_date="2026-09-05").price_change_reason == "MISSING_REFERENCE"


def test_tracker_trims_each_symbol_to_the_configured_minute_horizon():
    tracker = MinuteWindowTracker(keep_minutes=2)
    tracker.observe("600519", price_milli=1000, amount_yuan=100, source_time_ms=_ms("2026-09-04T01:18:00+00:00"))
    tracker.observe("600519", price_milli=1000, amount_yuan=120, source_time_ms=_ms("2026-09-04T01:19:00+00:00"))
    tracker.observe("600519", price_milli=1000, amount_yuan=140, source_time_ms=_ms("2026-09-04T01:20:00+00:00"))
    assert tracker.get_metrics("600519", trade_date="2026-09-04", minute_index=558) is None
    assert tracker.get_metrics("600519", trade_date="2026-09-04", minute_index=559) is not None
    assert tracker.get_metrics("600519", trade_date="2026-09-04", minute_index=560) is not None


def test_tracker_rejects_invalid_units_and_qualified_symbol_is_not_stripped():
    tracker = MinuteWindowTracker()
    with pytest.raises(ValueError):
        tracker.observe("600519", price_milli=0, amount_yuan=1, source_time_ms=1)
    with pytest.raises(ValueError):
        tracker.observe("600519", price_milli=1, amount_yuan=-1, source_time_ms=1)
    metric = tracker.observe("SH.600519", price_milli=1, amount_yuan=1, source_time_ms=_ms("2026-09-04T01:20:00+00:00"))
    assert metric.symbol == "SH.600519"
