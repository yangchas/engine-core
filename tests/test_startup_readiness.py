from datetime import datetime, timezone
from typing import Optional

import pytest

from engine_core import (
    DataResult,
    DataStatus,
    FreshnessPolicy,
    TimerSpec,
    TradingCalendarSnapshot,
    assess_startup_readiness,
    build_a_share_session_plan,
    build_calendar_snapshot,
    build_q2_projection,
    local_datetime_ms,
)
from examples.run_startup_readiness_probe import _load_calendar, run_probe


TRADE_DATE = "2026-09-08"
NOW = local_datetime_ms(TRADE_DATE, "09:26:00")


def _calendar() -> TradingCalendarSnapshot:
    return build_calendar_snapshot(
        [TRADE_DATE],
        version="startup-readiness-v1",
        declared_valid_from=TRADE_DATE,
        declared_valid_to=TRADE_DATE,
        source_guard_valid_from=TRADE_DATE,
        source_guard_valid_to=TRADE_DATE,
    )


def _q2(status_time_ms: int = NOW, *, observed_time_ms: Optional[int] = None):
    observed_ms = NOW if observed_time_ms is None else observed_time_ms
    observed = datetime.fromtimestamp(observed_ms / 1000, tz=timezone.utc)
    return build_q2_projection(
        TRADE_DATE,
        observed,
        ("000001",),
        {
            "000001": {
                "px": "1000",
                "pc": "990",
                "amt": "100000",
                "ts": str(status_time_ms),
                "mk": "sz",
            }
        },
        freshness_policy=FreshnessPolicy(stale_after_ms=60_000),
    )


def _reference(status=DataStatus.READY, *, available_at_ms=NOW):
    return DataResult(
        request_id="prev-request",
        function_id="previous_day_stats",
        status=status,
        data={
            "previous_trade_date": "2026-09-07",
            "close_by_symbol": {"000001": 10.0},
            "amount_by_symbol": {"000001": 100},
            "row_count": 1,
        },
        actual_source="fixture",
        requested_trade_date=TRADE_DATE,
        actual_trade_date="2026-09-07",
        effective_at_ms=None,
        available_at_ms=available_at_ms,
        observed_at_ms=NOW + 30_000,
        schema_version=1,
        completeness=1.0 if status is DataStatus.READY else 0.5,
    )


def test_startup_readiness_reports_due_timer_and_ready_inputs():
    calendar = _calendar()
    result = assess_startup_readiness(
        TRADE_DATE,
        NOW,
        calendar,
        build_a_share_session_plan(TRADE_DATE, calendar),
        q2=_q2(),
        required_reference_functions=("previous_day_stats",),
        reference_results={"previous_day_stats": _reference()},
        timer_specs=(TimerSpec("AUCTION_0926", "09:26:00"),),
    )
    assert result.status == "READY"
    assert result.phase == "AUCTION"
    assert result.q2_status == "READY"
    assert result.q2_content_hash == _q2().content_hash
    assert result.reference_statuses == (("previous_day_stats", "READY"),)
    assert result.reference_content_hashes[0][0] == "previous_day_stats"
    assert result.reference_content_hashes[0][1] == _reference().content_hash
    assert result.due_timer_ids == ("AUCTION_0926",)
    assert result.actions == ("DISPATCH_TIMER:AUCTION_0926",)
    assert result.content_hash


def test_missing_q2_blocks_without_calling_any_provider():
    calendar = _calendar()
    result = assess_startup_readiness(
        TRADE_DATE,
        local_datetime_ms(TRADE_DATE, "09:15:00"),
        calendar,
        build_a_share_session_plan(TRADE_DATE, calendar),
        q2=None,
        timer_specs=(TimerSpec("AUCTION_0926", "09:26:00"),),
    )
    assert result.status == "BLOCKED"
    assert result.q2_status == "MISSING"
    assert result.due_timer_ids == ()
    assert result.actions == ("WAIT_FOR_Q2",)


def test_blocked_q2_defers_due_timer_instead_of_advertising_dispatch():
    calendar = _calendar()
    result = assess_startup_readiness(
        TRADE_DATE,
        NOW,
        calendar,
        build_a_share_session_plan(TRADE_DATE, calendar),
        q2=None,
        timer_specs=(TimerSpec("AUCTION_0926", "09:26:00"),),
    )
    assert result.status == "BLOCKED"
    assert result.due_timer_ids == ("AUCTION_0926",)
    assert result.actions == ("WAIT_FOR_Q2", "DEFER_TIMER:AUCTION_0926")


def test_partial_q2_and_missing_reference_are_explicit_partial():
    calendar = _calendar()
    q2 = _q2(status_time_ms=NOW - 120_000, observed_time_ms=NOW)
    result = assess_startup_readiness(
        TRADE_DATE,
        NOW,
        calendar,
        build_a_share_session_plan(TRADE_DATE, calendar),
        q2=q2,
        required_reference_functions=("previous_day_stats", "theme_members"),
        reference_results={"previous_day_stats": _reference()},
    )
    assert result.status == "PARTIAL"
    assert result.q2_status == "STALE"
    assert result.reference_statuses == (
        ("previous_day_stats", "READY"),
        ("theme_members", "MISSING"),
    )
    assert result.actions == ("REFRESH_Q2", "PREFETCH:theme_members")
    assert "q2_status:STALE" in result.reasons


def test_unknown_reference_availability_is_not_treated_as_ready():
    calendar = _calendar()
    result = assess_startup_readiness(
        TRADE_DATE,
        NOW,
        calendar,
        build_a_share_session_plan(TRADE_DATE, calendar),
        q2=_q2(),
        required_reference_functions=("previous_day_stats",),
        reference_results={
            "previous_day_stats": _reference(available_at_ms=None),
        },
    )
    assert result.status == "PARTIAL"
    assert result.reference_statuses == (("previous_day_stats", "UNAVAILABLE"),)
    assert result.actions == ("PREFETCH:previous_day_stats",)
    assert "reference_status:previous_day_stats:UNAVAILABLE" in result.reasons


def test_future_reference_availability_is_not_cutoff_safe():
    calendar = _calendar()
    result = assess_startup_readiness(
        TRADE_DATE,
        NOW,
        calendar,
        build_a_share_session_plan(TRADE_DATE, calendar),
        q2=_q2(),
        required_reference_functions=("previous_day_stats",),
        reference_results={
            "previous_day_stats": _reference(available_at_ms=NOW + 1),
        },
    )
    assert result.reference_statuses == (("previous_day_stats", "UNAVAILABLE"),)


def test_q2_observed_or_source_time_after_cutoff_is_blocked():
    calendar = _calendar()
    future = _q2(status_time_ms=NOW + 1_000, observed_time_ms=NOW + 1_000)
    result = assess_startup_readiness(
        TRADE_DATE,
        NOW,
        calendar,
        build_a_share_session_plan(TRADE_DATE, calendar),
        q2=future,
    )
    assert result.status == "BLOCKED"
    assert result.q2_status == "UNAVAILABLE"
    assert result.actions == ("WAIT_FOR_Q2",)
    assert result.reasons == ("q2_observation_after_cutoff",)


def test_readiness_hash_tracks_input_content_identity():
    calendar = _calendar()
    plan = build_a_share_session_plan(TRADE_DATE, calendar)
    base = assess_startup_readiness(
        TRADE_DATE,
        NOW,
        calendar,
        plan,
        q2=_q2(),
        required_reference_functions=("previous_day_stats",),
        reference_results={"previous_day_stats": _reference()},
    )
    changed_reference = _reference()
    changed_reference = DataResult(
        request_id=changed_reference.request_id,
        function_id=changed_reference.function_id,
        status=changed_reference.status,
        data={
            "previous_trade_date": "2026-09-07",
            "close_by_symbol": {"000001": 10.01},
            "amount_by_symbol": {"000001": 100},
            "row_count": 1,
        },
        actual_source=changed_reference.actual_source,
        requested_trade_date=changed_reference.requested_trade_date,
        actual_trade_date=changed_reference.actual_trade_date,
        effective_at_ms=changed_reference.effective_at_ms,
        available_at_ms=changed_reference.available_at_ms,
        observed_at_ms=changed_reference.observed_at_ms,
        schema_version=changed_reference.schema_version,
        completeness=changed_reference.completeness,
    )
    changed = assess_startup_readiness(
        TRADE_DATE,
        NOW,
        calendar,
        plan,
        q2=_q2(),
        required_reference_functions=("previous_day_stats",),
        reference_results={"previous_day_stats": changed_reference},
    )
    assert changed.reference_content_hashes != base.reference_content_hashes
    assert changed.content_hash != base.content_hash


def test_startup_identity_and_calendar_mismatch_fail_closed():
    calendar = _calendar()
    other = build_calendar_snapshot(
        [TRADE_DATE],
        version="other-calendar",
        declared_valid_from=TRADE_DATE,
        declared_valid_to=TRADE_DATE,
        source_guard_valid_from=TRADE_DATE,
        source_guard_valid_to=TRADE_DATE,
    )
    with pytest.raises(ValueError, match="calendar identity"):
        assess_startup_readiness(
            TRADE_DATE,
            NOW,
            calendar,
            build_a_share_session_plan(TRADE_DATE, other),
            q2=_q2(),
        )


def test_reference_results_require_exact_request_trade_date():
    calendar = _calendar()
    wrong = _reference()
    wrong = DataResult(
        request_id=wrong.request_id,
        function_id=wrong.function_id,
        status=wrong.status,
        data=wrong.data,
        actual_source=wrong.actual_source,
        requested_trade_date="2026-09-07",
        actual_trade_date=wrong.actual_trade_date,
        effective_at_ms=wrong.effective_at_ms,
        available_at_ms=wrong.available_at_ms,
        observed_at_ms=wrong.observed_at_ms,
        schema_version=wrong.schema_version,
        completeness=wrong.completeness,
    )
    result = assess_startup_readiness(
        TRADE_DATE,
        NOW,
        calendar,
        build_a_share_session_plan(TRADE_DATE, calendar),
        q2=_q2(),
        required_reference_functions=("previous_day_stats",),
        reference_results={"previous_day_stats": wrong},
    )
    assert result.reference_statuses == (("previous_day_stats", "INVALID"),)


def test_real_probe_wrapper_only_reads_q2_and_exposes_readiness():
    class ReadClient:
        def smembers(self, key):
            return ["000001"]

        def hgetall(self, key):
            return {
                "px": "1000",
                "pc": "990",
                "amt": "100000",
                "ts": str(NOW),
            }

    result = run_probe(
        client=ReadClient(),
        trade_date=TRADE_DATE,
        calendar=_calendar(),
        observed_at=datetime.fromtimestamp(NOW / 1000, tz=timezone.utc),
        stale_after_ms=60_000,
        timer_specs=(TimerSpec("AUCTION_0926", "09:26:00"),),
    )
    assert result["read_only"] is True
    assert result["readiness"]["q2_status"] == "READY"
    assert result["readiness"]["due_timer_ids"] == ("AUCTION_0926",)


def test_startup_probe_calendar_loader_accepts_raw_probe_shape(tmp_path):
    path = tmp_path / "real-calendar-probe.json"
    path.write_text(
        '{"contract_version":"RealCalendarProbeV1",'
        '"version":"baostock-test-v1",'
        '"query_start":"2026-09-07",'
        '"query_end":"2026-09-09",'
        '"declared_valid_from":"2026-09-07",'
        '"declared_valid_to":"2026-09-09",'
        '"trading_dates":["2026-09-08"]}',
        encoding="utf-8",
    )

    snapshot = _load_calendar(path)

    assert snapshot.calendar_id == "CN_A_SHARE"
    assert snapshot.timezone_name == "Asia/Shanghai"
    assert snapshot.source_guard_valid_from == "2026-09-07"
    assert snapshot.source_guard_valid_to == "2026-09-09"
    assert snapshot.is_trading_day("2026-09-08")


def test_startup_probe_calendar_loader_rejects_probe_hash_mismatch(tmp_path):
    path = tmp_path / "bad-calendar-probe.json"
    path.write_text(
        '{"contract_version":"RealCalendarProbeV1",'
        '"version":"baostock-test-v1",'
        '"query_start":"2026-09-07",'
        '"query_end":"2026-09-09",'
        '"declared_valid_from":"2026-09-07",'
        '"declared_valid_to":"2026-09-09",'
        '"trading_dates":["2026-09-08"],'
        '"calendar_semantic_hash":"not-the-rebuilt-hash"}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="semantic hash"):
        _load_calendar(path)
