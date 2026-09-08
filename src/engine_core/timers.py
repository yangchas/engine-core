"""Pure once-only timer calculations for one SessionPlan.

This module determines which declared business timers are due between two
explicit logical times.  It does not read a clock, persist fired identities,
submit Engine signals, or encode producer-specific snapshot settling gates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .clock import local_datetime_ms, parse_clock_time_ms
from .contracts import semantic_hash
from .session import SessionPlan


TIMER_CONTRACT_VERSION = "SessionTimerV1"
TIMER_ORIGINS = {"NORMAL", "RECOVERY_CATCHUP"}


@dataclass(frozen=True)
class TimerSpec:
    """One business timer anchored to a strict local clock time."""

    timer_id: str
    scheduled_time: str
    trigger_basis: str = "WALL_DEADLINE"
    scheduled_ms_of_day: int = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.timer_id, str) or not self.timer_id.strip():
            raise ValueError("timer_id is required")
        if not isinstance(self.trigger_basis, str) or not self.trigger_basis.strip():
            raise ValueError("trigger_basis is required")
        object.__setattr__(self, "timer_id", self.timer_id.strip())
        object.__setattr__(self, "trigger_basis", self.trigger_basis.strip().upper())
        object.__setattr__(
            self,
            "scheduled_ms_of_day",
            parse_clock_time_ms(self.scheduled_time),
        )

    def scheduled_time_ms(self, plan: SessionPlan) -> int:
        return local_datetime_ms(
            plan.trade_date,
            self.scheduled_time,
            timezone_name=plan.timezone_name,
        )


@dataclass(frozen=True)
class TimerFiring:
    """One deterministic due-timer result."""

    timer_id: str
    scheduled_time_ms: int
    fired_time_ms: int
    trigger_basis: str
    origin: str
    late_by_ms: int
    session_plan_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.origin not in TIMER_ORIGINS:
            raise ValueError("timer origin must be NORMAL or RECOVERY_CATCHUP")
        if self.scheduled_time_ms <= 0 or self.fired_time_ms <= 0:
            raise ValueError("timer times must be positive")
        if self.fired_time_ms < self.scheduled_time_ms:
            raise ValueError("timer cannot fire before its scheduled time")
        if self.late_by_ms != self.fired_time_ms - self.scheduled_time_ms:
            raise ValueError("late_by_ms must match timer times")
        object.__setattr__(
            self,
            "content_hash",
            semantic_hash(
                {
                    "contract_version": TIMER_CONTRACT_VERSION,
                    "timer_id": self.timer_id,
                    "scheduled_time_ms": self.scheduled_time_ms,
                    "fired_time_ms": self.fired_time_ms,
                    "trigger_basis": self.trigger_basis,
                    "origin": self.origin,
                    "late_by_ms": self.late_by_ms,
                    "session_plan_hash": self.session_plan_hash,
                }
            ),
        )


def due_timer_firings(
    plan: SessionPlan,
    specs: Iterable[TimerSpec],
    *,
    previous_time_ms: Optional[int],
    current_time_ms: int,
    already_fired: Iterable[str] = (),
    origin: str = "NORMAL",
) -> tuple[TimerFiring, ...]:
    """Return not-yet-fired timers due in ``[previous, current]``.

    The lower boundary is inclusive so a crash after advancing the logical
    frontier but before recording a firing does not lose the timer.  The
    caller-provided ``already_fired`` identities provide once-only behavior.
    On a cold recovery, pass ``previous_time_ms=None`` and
    ``origin='RECOVERY_CATCHUP'`` to evaluate timers from local midnight.
    """

    if origin not in TIMER_ORIGINS:
        raise ValueError("origin must be NORMAL or RECOVERY_CATCHUP")
    if isinstance(current_time_ms, bool) or not isinstance(current_time_ms, int):
        raise TypeError("current_time_ms must be an integer")
    plan.phase_at_ms(current_time_ms)
    if previous_time_ms is not None:
        if isinstance(previous_time_ms, bool) or not isinstance(previous_time_ms, int):
            raise TypeError("previous_time_ms must be an integer or None")
        if previous_time_ms <= 0:
            raise ValueError("previous_time_ms must be positive")
        if previous_time_ms > current_time_ms:
            raise ValueError("timer frontier cannot move backwards")

    supplied_specs = tuple(specs)
    if any(not isinstance(item, TimerSpec) for item in supplied_specs):
        raise TypeError("specs must contain TimerSpec values")
    ordered_specs = tuple(
        sorted(supplied_specs, key=lambda item: (item.scheduled_ms_of_day, item.timer_id))
    )
    timer_ids = tuple(item.timer_id for item in ordered_specs)
    if len(timer_ids) != len(set(timer_ids)):
        raise ValueError("timer_id values must be unique")

    fired_ids = tuple(already_fired)
    if any(not isinstance(item, str) or not item for item in fired_ids):
        raise ValueError("already_fired must contain non-empty timer ids")
    if len(fired_ids) != len(set(fired_ids)):
        raise ValueError("already_fired must not contain duplicates")
    unknown_fired = set(fired_ids).difference(timer_ids)
    if unknown_fired:
        raise ValueError("already_fired contains an unknown timer_id")

    day_start_ms = local_datetime_ms(
        plan.trade_date,
        "00:00:00",
        timezone_name=plan.timezone_name,
    )
    lower_bound_ms = day_start_ms if previous_time_ms is None else max(
        day_start_ms,
        previous_time_ms,
    )
    fired = set(fired_ids)
    results = []
    for spec in ordered_specs:
        scheduled_time_ms = spec.scheduled_time_ms(plan)
        if spec.timer_id in fired:
            continue
        if lower_bound_ms <= scheduled_time_ms <= current_time_ms:
            results.append(
                TimerFiring(
                    timer_id=spec.timer_id,
                    scheduled_time_ms=scheduled_time_ms,
                    fired_time_ms=current_time_ms,
                    trigger_basis=spec.trigger_basis,
                    origin=origin,
                    late_by_ms=current_time_ms - scheduled_time_ms,
                    session_plan_hash=plan.content_hash,
                )
            )
    return tuple(results)
