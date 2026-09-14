from datetime import date, datetime, time, timezone
import json
from pathlib import Path

import pytest

from engine_core import (
    CalendarCoverageError,
    CalendarSnapshotError,
    TradingCalendarSnapshot,
    build_calendar_snapshot,
    parse_trade_date,
    validate_calendar_version_identity,
)


def _calendar():
    return build_calendar_snapshot(
        ["2023-12-29", "2024-01-02", "2024-01-03", "2024-01-04", "2026-12-31", "2027-01-04"],
        version="baostock-20260907-v1",
        declared_valid_from="2024-01-01",
        declared_valid_to="2026-12-31",
        source_guard_valid_from="2023-12-01",
        source_guard_valid_to="2027-01-31",
        source_id="baostock",
        observed_at_ms=1788710400000,
        evidence_ref="calendar://cn-a-share/baostock-20260907-v1",
    )


def test_parse_trade_date_rejects_datetime_and_loose_strings():
    assert parse_trade_date("2024-01-02") == date(2024, 1, 2)
    assert parse_trade_date(date(2024, 1, 2)) == date(2024, 1, 2)
    with pytest.raises(TypeError):
        parse_trade_date(datetime(2024, 1, 2))
    with pytest.raises(ValueError):
        parse_trade_date("2024-1-2")


def test_previous_and_next_trade_days_use_guard_dates_at_declared_edges():
    calendar = _calendar()
    assert calendar.declared_from == date(2024, 1, 1)
    assert calendar.declared_to == date(2026, 12, 31)
    assert calendar.guard_from == date(2023, 12, 1)
    assert calendar.guard_to == date(2027, 1, 31)
    assert calendar.previous_trade_day("2024-01-02") == date(2023, 12, 29)
    assert calendar.next_trade_day("2026-12-31") == date(2027, 1, 4)
    assert calendar.is_trading_day("2024-01-01") is False
    with pytest.raises(CalendarCoverageError):
        calendar.previous_trade_day("2023-12-29")
    with pytest.raises(CalendarCoverageError):
        calendar.next_trade_day("2027-01-04")


def test_latest_completed_trade_day_has_explicit_cutoff_and_timezone():
    calendar = _calendar()
    before = datetime(2024, 1, 3, 7, 29, tzinfo=timezone.utc)
    at_cutoff = datetime(2024, 1, 3, 7, 30, tzinfo=timezone.utc)
    assert calendar.latest_completed_trade_day(before) == date(2024, 1, 2)
    assert calendar.latest_completed_trade_day(at_cutoff) == date(2024, 1, 3)
    with pytest.raises(ValueError):
        calendar.latest_completed_trade_day(datetime(2024, 1, 3, 7, 30))


def test_holiday_and_weekend_flow_answers_previous_trade_date_for_data_queries():
    calendar = _calendar()

    # A holiday is a valid declared date, but it is not a trading-day request
    # for a data function.  The calendar still supplies the prior completed
    # trading date when the caller explicitly asks for that derived relation.
    assert calendar.is_trading_day("2024-01-01") is False
    assert calendar.previous_trade_day("2024-01-01") == date(2023, 12, 29)
    assert calendar.next_trade_day("2024-01-01") == date(2024, 1, 2)

    # The same rule applies across a weekend: callers get the last listed
    # trading date, never a fabricated Saturday/Sunday data date.
    assert calendar.is_trading_day("2024-01-06") is False
    assert calendar.previous_trade_day("2024-01-06") == date(2024, 1, 4)


def test_calendar_rejects_dates_outside_source_guard_before_answering_status():
    calendar = _calendar()
    with pytest.raises(CalendarCoverageError):
        calendar.is_trading_day("2023-11-30")
    with pytest.raises(CalendarCoverageError):
        calendar.is_trading_day("2027-02-01")


def test_latest_completed_trade_day_uses_previous_trade_day_on_holiday():
    calendar = _calendar()

    # 2024-01-01 is a holiday even after the completion cutoff, so it must not
    # be promoted to a completed trading day.
    holiday_after_cutoff = datetime(2024, 1, 1, 8, 0, tzinfo=timezone.utc)
    assert calendar.latest_completed_trade_day(holiday_after_cutoff) == date(2023, 12, 29)


def test_calendar_semantic_hash_ignores_evidence_but_version_identity_detects_content_change():
    left = _calendar()
    right = build_calendar_snapshot(
        list(left.trading_dates),
        version=left.version,
        declared_valid_from=left.declared_valid_from,
        declared_valid_to=left.declared_valid_to,
        source_guard_valid_from=left.source_guard_valid_from,
        source_guard_valid_to=left.source_guard_valid_to,
        source_id="other-source",
        observed_at_ms=left.observed_at_ms + 1,
        evidence_ref="calendar://other",
    )
    assert left.semantic_hash == right.semantic_hash
    assert left.evidence_hash != right.evidence_hash

    changed = build_calendar_snapshot(
        list(left.trading_dates) + ["2024-01-05"],
        version=left.version,
        declared_valid_from=left.declared_valid_from,
        declared_valid_to=left.declared_valid_to,
        source_guard_valid_from=left.source_guard_valid_from,
        source_guard_valid_to=left.source_guard_valid_to,
    )
    with pytest.raises(CalendarSnapshotError):
        validate_calendar_version_identity(left, changed)


def test_calendar_dates_must_be_sorted_unique_and_guarded():
    with pytest.raises(CalendarSnapshotError):
        TradingCalendarSnapshot(
            calendar_id="CN_A_SHARE",
            version="bad",
            timezone_name="Asia/Shanghai",
            declared_valid_from="2024-01-01",
            declared_valid_to="2024-12-31",
            source_guard_valid_from="2023-12-01",
            source_guard_valid_to="2025-01-31",
            trading_dates=("2024-01-03", "2024-01-02"),
        )


def test_baostock_calendar_fixture_loads_as_canonical_snapshot():
    payload = json.loads(
        (
            Path(__file__).parent
            / "fixtures/calendar/baostock_cn_a_share_20260907.json"
        ).read_text(encoding="utf-8")
    )
    snapshot = TradingCalendarSnapshot(
        calendar_id=payload["calendar_id"],
        version=payload["version"],
        timezone_name=payload["timezone"],
        declared_valid_from=payload["declared_valid_from"],
        declared_valid_to=payload["declared_valid_to"],
        source_guard_valid_from=payload["source_guard_valid_from"],
        source_guard_valid_to=payload["source_guard_valid_to"],
        trading_dates=tuple(payload["trading_dates"]),
        source_id=payload["source_id"],
        observed_at_ms=payload["observed_at_ms"],
        evidence_ref=payload["evidence_ref"],
    )
    assert len(snapshot.trading_dates) == 748
    assert snapshot.source_guard_valid_to == "2026-12-31"
    assert snapshot.semantic_hash == "58b658ce0e47e224ce98e9202e23255f43d411e93427981cec36c82f54677d44"
    assert snapshot.evidence_hash == "9c10961fa048436cbacbddad2892a690b40ebd3c987423e1e4519817ec96ca7a"
    with pytest.raises(CalendarCoverageError):
        snapshot.next_trade_day("2026-12-31")


def test_baostock_calendar_fixture_answers_current_and_holiday_date_questions():
    payload = json.loads(
        (
            Path(__file__).parent
            / "fixtures/calendar/baostock_cn_a_share_20260907.json"
        ).read_text(encoding="utf-8")
    )
    snapshot = TradingCalendarSnapshot(
        calendar_id=payload["calendar_id"],
        version=payload["version"],
        timezone_name=payload["timezone"],
        declared_valid_from=payload["declared_valid_from"],
        declared_valid_to=payload["declared_valid_to"],
        source_guard_valid_from=payload["source_guard_valid_from"],
        source_guard_valid_to=payload["source_guard_valid_to"],
        trading_dates=tuple(payload["trading_dates"]),
        source_id=payload["source_id"],
        observed_at_ms=payload["observed_at_ms"],
        evidence_ref=payload["evidence_ref"],
    )
    assert snapshot.is_trading_day("2026-09-09") is True
    assert snapshot.previous_trade_day("2026-09-09") == date(2026, 9, 8)
    assert snapshot.is_trading_day("2026-10-01") is False
    assert snapshot.previous_trade_day("2026-10-01") == date(2026, 9, 30)
