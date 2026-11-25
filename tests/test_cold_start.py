import pytest

from engine_core import (
    AuctionAnchorSessionV1,
    assess_cold_start,
    build_a_share_session_plan,
    build_calendar_snapshot,
    classify_start,
    local_datetime_ms,
)

D = "2026-10-14"


@pytest.fixture(scope="module")
def plan():
    cal = build_calendar_snapshot(
        [D], version="cold-start-test", declared_valid_from=D, declared_valid_to=D,
        source_guard_valid_from=D, source_guard_valid_to=D)
    return build_a_share_session_plan(D, cal)


def ms(hms):
    return local_datetime_ms(D, hms)


@pytest.mark.parametrize("hms,start_class", [
    ("08:00:00", "PREMARKET"), ("09:17:00", "AUCTION_COLLECTING"),
    ("09:25:10", "AUCTION_FINAL"), ("09:31:00", "OPENING_FORMING"),
    ("10:00:00", "MORNING_SESSION"), ("12:00:00", "LUNCH_BREAK"),
    ("14:00:00", "AFTERNOON_SESSION"), ("16:00:00", "POSTMARKET"),
])
def test_start_classes(plan, hms, start_class):
    assert classify_start(plan, ms(hms)) == start_class


def test_premarket_start_loses_nothing(plan):
    a = assess_cold_start(plan, ms("08:30:00"))
    assert a.status == "READY" and a.unrecoverable == ()
    assert set(a.anchors.values()) == {"PENDING"}
    assert a.late_final_recovery == "NOT_YET_APPLICABLE"
    assert a.recovery_due_timers == ()


def test_start_inside_auction_before_0924_still_gets_the_delta(plan):
    a = assess_cold_start(plan, ms("09:21:00"))
    assert a.anchors["0920"] == "OBSERVABLE_AT_START" and a.anchors["0924"] == "PENDING"
    assert a.theme_amount_delta_0924_0925 == "AVAILABLE_FROM_SESSION"
    assert a.status == "READY"


def test_start_after_0925_needs_td_for_amount_delta_and_keeps_recovery_open(plan):
    a = assess_cold_start(plan, ms("09:27:00"))
    assert a.late_final_recovery == "WINDOW_OPEN"
    assert a.theme_amount_delta_0924_0925 == "TD_RECONSTRUCTION_REQUIRED"
    assert a.opening_0932 == "PENDING"
    assert a.recovery_due_timers == ("AUCTION_0926",)
    assert a.status == "PARTIAL"


def test_start_after_open_cannot_recover_finals_or_opening_cutoff(plan):
    a = assess_cold_start(plan, ms("09:33:00"))
    assert a.late_final_recovery == "WINDOW_CLOSED"
    assert a.opening_0932 == "TD_RECONSTRUCTION_REQUIRED"
    assert a.recovery_due_timers == ("AUCTION_0926", "OPENING_0932")
    assert any("late_final_recovery" in item for item in a.unrecoverable)
    assert any("opening_0932" in item for item in a.unrecoverable)


def test_recovery_window_matches_the_session_implementation(plan):
    session = AuctionAnchorSessionV1(D, ("000001",), recover_final_from_phase_zero=True)
    lower, upper = session._final_lower_ms, session._final_upper_ms
    assert assess_cold_start(plan, lower - 1).late_final_recovery == "NOT_YET_APPLICABLE"
    assert assess_cold_start(plan, lower).late_final_recovery == "WINDOW_OPEN"
    assert assess_cold_start(plan, upper - 1).late_final_recovery == "WINDOW_OPEN"
    assert assess_cold_start(plan, upper).late_final_recovery == "WINDOW_CLOSED"


def test_assessment_is_deterministic_and_never_backdates(plan):
    a = assess_cold_start(plan, ms("10:00:00"))
    b = assess_cold_start(plan, ms("10:00:00"))
    assert a.content_hash == b.content_hash
    assert a.start_ms == ms("10:00:00")
    assert a.as_mapping()["start_class"] == "MORNING_SESSION"
    assert a.anchor_semantics == (
        "OBSERVABLE_AT_START=按时钟可读, 未检查字段实际存在, 观察时间=启动时刻"
    )
    assert a.as_mapping()["anchor_semantics"] == a.anchor_semantics


def _rows(**symbols):
    base = {"price_milli": 10000, "volume_lots": 100, "amount_yuan": 1_000_000,
            "auction_anchor_0925_price_milli": 0}
    return {s: dict(base, **row) for s, row in symbols.items()}


def test_cold_started_session_recovers_visible_final_and_not_overwritten_rows():
    session = AuctionAnchorSessionV1(D, ("000001", "000002"), recover_final_from_phase_zero=True)
    rows = _rows(
        **{
            # final still visible (halted after the open): recoverable
            "000001": {"phase": 0, "source_record_time_ms": ms("09:26:03")},
            # overwritten by continuous trading after 09:30: not recoverable
            "000002": {"phase": 3, "source_record_time_ms": ms("09:41:10")},
        }
    )
    revision = session.observe("0925", rows, evaluation_time_ms=ms("10:00:00"))
    assert set(session.phase_zero_recoveries) == {"000001"}
    assert "000001" in revision.available_anchor_symbols
    assert "000002" in revision.missing_anchor_symbols
