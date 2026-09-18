from datetime import datetime, timezone
from dataclasses import replace

import pytest

from engine_core import (
    FreshnessPolicy,
    SessionRuntimeCoordinator,
    TimerSpec,
    build_a_share_session_plan,
    build_calendar_snapshot,
    build_q2_projection,
    local_datetime_ms,
)


TRADE_DATE = "2026-09-08"


def _calendar():
    return build_calendar_snapshot(
        [TRADE_DATE],
        version="runtime-coordinator-v1",
        declared_valid_from=TRADE_DATE,
        declared_valid_to=TRADE_DATE,
        source_guard_valid_from=TRADE_DATE,
        source_guard_valid_to=TRADE_DATE,
    )


def _q2(as_of_ms: int, *, stale_after_ms: int = 60_000, source_time_ms=None):
    source_time_ms = as_of_ms if source_time_ms is None else source_time_ms
    observed = datetime.fromtimestamp(as_of_ms / 1000, tz=timezone.utc)
    return build_q2_projection(
        TRADE_DATE,
        observed,
        ("000001",),
        {
            "000001": {
                "px": "1000",
                "pc": "990",
                "amt": "100000",
                "ts": str(source_time_ms),
                "mk": "sz",
            }
        },
        freshness_policy=FreshnessPolicy(stale_after_ms=stale_after_ms),
    )


def _coordinator():
    calendar = _calendar()
    return SessionRuntimeCoordinator(
        trade_date=TRADE_DATE,
        calendar=calendar,
        session_plan=build_a_share_session_plan(TRADE_DATE, calendar),
        timer_specs=(TimerSpec("AUCTION_0926", "09:26:00"),),
    )


def test_blocked_q2_defers_then_ready_q2_dispatches_and_acknowledges_once():
    at_0926 = local_datetime_ms(TRADE_DATE, "09:26:00")
    coordinator = _coordinator()

    blocked = coordinator.poll(as_of_ms=at_0926, q2=None)
    assert blocked.readiness.status == "BLOCKED"
    assert blocked.dispatchable_firings == ()
    assert blocked.deferred_timer_ids == ("AUCTION_0926",)

    ready = coordinator.poll(as_of_ms=at_0926 + 1_000, q2=_q2(at_0926 + 1_000))
    assert ready.readiness.status == "READY"
    assert tuple(item.timer_id for item in ready.dispatchable_firings) == ("AUCTION_0926",)
    firing = ready.dispatchable_firings[0]
    coordinator.acknowledge(firing)
    coordinator.acknowledge(firing)  # idempotent duplicate acknowledgement

    after = coordinator.poll(as_of_ms=at_0926 + 2_000, q2=_q2(at_0926 + 2_000))
    assert after.dispatchable_firings == ()
    assert coordinator.completed_timer_ids == ("AUCTION_0926",)


def test_recovery_poll_preserves_origin_and_does_not_re_register_after_ack():
    at_0928 = local_datetime_ms(TRADE_DATE, "09:28:00")
    coordinator = _coordinator()
    observed = coordinator.poll(
        as_of_ms=at_0928,
        q2=_q2(at_0928),
        origin="RECOVERY_CATCHUP",
    )
    assert observed.readiness.status == "READY"
    assert len(observed.dispatchable_firings) == 1
    firing = observed.dispatchable_firings[0]
    assert firing.origin == "RECOVERY_CATCHUP"
    coordinator.acknowledge(firing)

    later = coordinator.poll(
        as_of_ms=local_datetime_ms(TRADE_DATE, "09:32:00"),
        q2=_q2(local_datetime_ms(TRADE_DATE, "09:32:00")),
        origin="RECOVERY_CATCHUP",
    )
    assert later.dispatchable_firings == ()


def test_partial_q2_can_be_observed_without_being_promoted_to_ready():
    as_of = local_datetime_ms(TRADE_DATE, "09:26:00")
    coordinator = _coordinator()
    result = coordinator.poll(
        as_of_ms=as_of,
        q2=_q2(as_of, stale_after_ms=1, source_time_ms=as_of - 10_000),
    )
    assert result.readiness.status == "PARTIAL"
    assert result.readiness.q2_status == "STALE"
    assert len(result.dispatchable_firings) == 1


def test_runtime_rejects_backwards_observation_and_conflicting_ack():
    as_of = local_datetime_ms(TRADE_DATE, "09:26:00")
    coordinator = _coordinator()
    result = coordinator.poll(as_of_ms=as_of, q2=_q2(as_of))
    firing = result.dispatchable_firings[0]
    with pytest.raises(ValueError, match="does not match pending"):
        coordinator.acknowledge(
            replace(
                firing,
                fired_time_ms=firing.fired_time_ms + 1,
                late_by_ms=firing.late_by_ms + 1,
            )
        )
    coordinator.acknowledge(firing)
    with pytest.raises(ValueError, match="cannot move backwards"):
        coordinator.poll(as_of_ms=as_of - 1, q2=_q2(as_of - 1))


def test_runtime_session_identity_is_validated_at_construction():
    calendar = _calendar()
    with pytest.raises(ValueError, match="trade_date does not match"):
        SessionRuntimeCoordinator(
            trade_date="2026-09-09",
            calendar=calendar,
            session_plan=build_a_share_session_plan(TRADE_DATE, calendar),
            timer_specs=(),
        )
