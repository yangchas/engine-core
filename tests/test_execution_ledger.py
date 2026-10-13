import json

import pytest

from engine_core import (
    ExecutionLedger,
    TimerSpec,
    build_a_share_session_plan,
    build_calendar_snapshot,
    due_timer_firings,
    local_datetime_ms,
)
from engine_core.session_runtime import SessionRuntimeCoordinator

D = "2026-10-14"


def _plan_cal():
    cal = build_calendar_snapshot(
        [D], version="ledger-test", declared_valid_from=D, declared_valid_to=D,
        source_guard_valid_from=D, source_guard_valid_to=D)
    return build_a_share_session_plan(D, cal), cal


def _firings(hms="09:33:00"):
    plan, _ = _plan_cal()
    return due_timer_firings(
        plan, (TimerSpec("AUCTION_0926", "09:26:00"), TimerSpec("OPENING_0932", "09:32:00")),
        previous_time_ms=None, current_time_ms=local_datetime_ms(D, hms),
        origin="RECOVERY_CATCHUP")


def test_record_is_idempotent_and_survives_reopen(tmp_path):
    path = tmp_path / "ledger.jsonl"
    first, second = _firings()
    ledger = ExecutionLedger(path)
    assert ledger.record(D, first) is True
    assert ledger.record(D, first) is False
    assert ledger.record(D, second) is True
    reopened = ExecutionLedger(path)
    assert reopened.completed_timer_ids(D) == ("AUCTION_0926", "OPENING_0932")
    assert reopened.completed_timer_ids("2026-10-15") == ()
    assert [f.content_hash for f in reopened.firings(D)] == [first.content_hash, second.content_hash]


def test_conflicting_firing_for_a_completed_timer_is_rejected(tmp_path):
    ledger = ExecutionLedger(tmp_path / "l.jsonl")
    early = _firings("09:27:00")[0]
    late = _firings("09:40:00")[0]  # same timer id, different fired time
    assert early.timer_id == late.timer_id and early.content_hash != late.content_hash
    ledger.record(D, early)
    with pytest.raises(ValueError):
        ledger.record(D, late)


def test_torn_line_is_skipped_counted_and_does_not_glue_onto_next_record(tmp_path):
    path = tmp_path / "l.jsonl"
    first, second = _firings()
    ledger = ExecutionLedger(path)
    ledger.record(D, first)
    with path.open("ab") as handle:
        handle.write(b'{"contract":"TimerExecutionLedgerV1","trade_da')  # crash mid-write
    assert ledger.unreadable_line_count() == 1
    assert ledger.completed_timer_ids(D) == ("AUCTION_0926",)
    assert ledger.record(D, second) is True
    assert ledger.completed_timer_ids(D) == ("AUCTION_0926", "OPENING_0932")
    assert ledger.unreadable_line_count() == 1


def test_tampered_record_hash_is_detected(tmp_path):
    path = tmp_path / "l.jsonl"
    first, _ = _firings()
    ExecutionLedger(path).record(D, first)
    record = json.loads(path.read_text())
    record["late_by_ms"] += 1
    path.write_text(json.dumps(record) + "\n")
    with pytest.raises(ValueError):
        ExecutionLedger(path).firings(D)


def test_restart_does_not_refire_completed_timers(tmp_path):
    plan, cal = _plan_cal()
    specs = (TimerSpec("AUCTION_0926", "09:26:00"), TimerSpec("OPENING_0932", "09:32:00"))
    ledger = ExecutionLedger(tmp_path / "l.jsonl")
    for firing in _firings("09:27:00"):  # only AUCTION_0926 was due and acknowledged
        ledger.record(D, firing)

    restarted = SessionRuntimeCoordinator(
        trade_date=D, calendar=cal, session_plan=plan, timer_specs=specs)
    assert restarted.restore_completed(ledger.firings(D)) == 1
    assert restarted.completed_timer_ids == ("AUCTION_0926",)
    due = due_timer_firings(
        plan, specs, previous_time_ms=None, current_time_ms=local_datetime_ms(D, "09:33:00"),
        already_fired=restarted.completed_timer_ids, origin="RECOVERY_CATCHUP")
    assert [f.timer_id for f in due] == ["OPENING_0932"]
    # restoring twice or restoring a foreign plan is safe / rejected
    assert restarted.restore_completed(ledger.firings(D)) == 0
