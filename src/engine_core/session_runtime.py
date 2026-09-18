"""Minimal in-memory startup and timer coordination for Core integration.

This module is intentionally smaller than a scheduler or workflow engine.  It
only composes the already-tested calendar, session, timer, and readiness
wheels.  Provider I/O, Engine signal submission, persistence, and effects stay
with the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Tuple

from .calendar import TradingCalendarSnapshot, parse_trade_date
from .contracts import DataResult, semantic_hash
from .q2 import Q2ProjectionSnapshot
from .session import SessionPlan
from .startup_readiness import StartupReadiness, assess_startup_readiness
from .timers import TimerFiring, TimerSpec, due_timer_firings


SESSION_RUNTIME_CONTRACT_VERSION = "SessionRuntimeCoordinatorV1"


@dataclass(frozen=True)
class RuntimePoll:
    """One deterministic observation of startup readiness and due timers."""

    trade_date: str
    as_of_ms: int
    readiness: StartupReadiness
    dispatchable_firings: Tuple[TimerFiring, ...]
    deferred_timer_ids: Tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if isinstance(self.as_of_ms, bool) or not isinstance(self.as_of_ms, int):
            raise TypeError("as_of_ms must be an integer")
        if self.as_of_ms <= 0:
            raise ValueError("as_of_ms must be positive")
        if not isinstance(self.readiness, StartupReadiness):
            raise TypeError("readiness must be StartupReadiness")
        if self.trade_date != self.readiness.trade_date:
            raise ValueError("trade_date does not match readiness trade_date")
        dispatchable = tuple(self.dispatchable_firings)
        deferred = tuple(self.deferred_timer_ids)
        if any(not isinstance(item, TimerFiring) for item in dispatchable):
            raise TypeError("dispatchable_firings must contain TimerFiring values")
        if any(not isinstance(item, str) or not item for item in deferred):
            raise ValueError("deferred_timer_ids must contain non-empty ids")
        if len(deferred) != len(set(deferred)):
            raise ValueError("deferred_timer_ids must be unique")
        if set(deferred).intersection(item.timer_id for item in dispatchable):
            raise ValueError("a timer cannot be both deferred and dispatchable")
        object.__setattr__(self, "dispatchable_firings", dispatchable)
        object.__setattr__(self, "deferred_timer_ids", deferred)
        object.__setattr__(
            self,
            "content_hash",
            semantic_hash(
                {
                    "contract_version": SESSION_RUNTIME_CONTRACT_VERSION,
                    "trade_date": self.trade_date,
                    "as_of_ms": self.as_of_ms,
                    "readiness_hash": self.readiness.content_hash,
                    "dispatchable": tuple(
                        (item.timer_id, item.content_hash)
                        for item in dispatchable
                    ),
                    "deferred_timer_ids": deferred,
                }
            ),
        )


class SessionRuntimeCoordinator:
    """Small, non-persistent coordinator for one explicit trading session.

    ``poll`` never performs I/O.  The caller supplies the already-observed Q2
    and reference results, then submits returned ``TimerFiring`` values to the
    Engine.  The caller calls ``acknowledge`` only after that submission is
    accepted.  This keeps timer identity separate from signal/effect delivery
    and makes a failed dispatch retryable without adding a workflow engine.
    """

    def __init__(
        self,
        *,
        trade_date: str,
        calendar: TradingCalendarSnapshot,
        session_plan: SessionPlan,
        timer_specs: Iterable[TimerSpec],
        required_reference_functions: Iterable[str] = (),
        q2_optional_timer_ids: Iterable[str] = (),
    ) -> None:
        parsed = parse_trade_date(trade_date)
        if parsed.isoformat() != trade_date:
            raise ValueError("trade_date must be strict YYYY-MM-DD")
        if not isinstance(calendar, TradingCalendarSnapshot):
            raise TypeError("calendar must be TradingCalendarSnapshot")
        if not isinstance(session_plan, SessionPlan):
            raise TypeError("session_plan must be SessionPlan")
        if session_plan.trade_date != trade_date:
            raise ValueError("session_plan trade_date does not match trade_date")
        if (
            session_plan.calendar_id != calendar.calendar_id
            or session_plan.calendar_version != calendar.version
            or session_plan.calendar_semantic_hash != calendar.semantic_hash
        ):
            raise ValueError("session_plan calendar identity does not match calendar")
        if not calendar.is_trading_day(parsed):
            raise ValueError("trade_date must be a trading day")

        specs = tuple(timer_specs)
        if any(not isinstance(item, TimerSpec) for item in specs):
            raise TypeError("timer_specs must contain TimerSpec values")
        if len({item.timer_id for item in specs}) != len(specs):
            raise ValueError("timer_specs must have unique timer ids")
        required = tuple(required_reference_functions)
        if any(not isinstance(item, str) or not item for item in required):
            raise ValueError("required_reference_functions must contain non-empty ids")
        if len(set(required)) != len(required):
            raise ValueError("required_reference_functions must be unique")
        q2_optional = tuple(q2_optional_timer_ids)
        if any(not isinstance(item, str) or not item for item in q2_optional):
            raise ValueError("q2_optional_timer_ids must contain non-empty ids")
        if len(set(q2_optional)) != len(q2_optional):
            raise ValueError("q2_optional_timer_ids must be unique")
        unknown_optional = set(q2_optional).difference(item.timer_id for item in specs)
        if unknown_optional:
            raise ValueError("q2_optional_timer_ids contains an unknown timer_id")

        self.trade_date = trade_date
        self.calendar = calendar
        self.session_plan = session_plan
        self.timer_specs = specs
        self.required_reference_functions = required
        self.q2_optional_timer_ids = q2_optional
        self._frontier_ms: Optional[int] = None
        self._last_poll_ms: Optional[int] = None
        self._fired: dict[str, TimerFiring] = {}
        self._pending: dict[str, TimerFiring] = {}
        self._deferred: set[str] = set()

    @property
    def frontier_ms(self) -> Optional[int]:
        """Last acknowledged timer frontier, not a persistence cursor."""

        return self._frontier_ms

    @property
    def completed_timer_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._fired))

    def poll(
        self,
        *,
        as_of_ms: int,
        q2: Optional[Q2ProjectionSnapshot],
        reference_results: Optional[Mapping[str, DataResult]] = None,
        origin: str = "NORMAL",
    ) -> RuntimePoll:
        """Assess readiness and return timers that may be submitted.

        A blocked readiness assessment returns due timer ids as deferred and no
        dispatchable firings.  A partial assessment remains dispatchable so a
        caller can apply node-specific policies (for example auction facts may
        tolerate stale Q2 while opening facts fail closed).
        """

        if isinstance(as_of_ms, bool) or not isinstance(as_of_ms, int):
            raise TypeError("as_of_ms must be an integer")
        if as_of_ms <= 0:
            raise ValueError("as_of_ms must be positive")
        if self._last_poll_ms is not None and as_of_ms < self._last_poll_ms:
            raise ValueError("runtime observation time cannot move backwards")

        logical_previous_ms = (
            None
            if self._pending or self._deferred
            else self._frontier_ms
        )
        readiness = assess_startup_readiness(
            self.trade_date,
            as_of_ms,
            self.calendar,
            self.session_plan,
            q2=q2,
            required_reference_functions=self.required_reference_functions,
            reference_results=reference_results,
            timer_specs=self.timer_specs,
            previous_time_ms=logical_previous_ms,
            already_fired=self._fired,
            origin=origin,
        )
        # A pending or deferred firing is still part of the current logical
        # frontier.  Re-evaluate from the day start so an earlier timer is not
        # lost when another timer at the same poll is acknowledged first.
        due = due_timer_firings(
            self.session_plan,
            self.timer_specs,
            previous_time_ms=logical_previous_ms,
            current_time_ms=as_of_ms,
            already_fired=self._fired,
            origin=origin,
        )

        if readiness.status == "BLOCKED":
            # Auction fact collection is independently TD-owned in the
            # current shadow path.  Explicitly configured q2-optional timers
            # may therefore dispatch when the only blocking reason is Q2;
            # all other blocked work remains deferred.  This is a small node
            # policy, not a workflow engine or fallback mechanism.
            q2_only_blocked = bool(readiness.reasons) and all(
                reason.startswith("q2_") for reason in readiness.reasons
            )
            if q2_only_blocked:
                dispatchable = tuple(
                    self._pending.setdefault(item.timer_id, item)
                    for item in due
                    if item.timer_id in self.q2_optional_timer_ids
                )
            else:
                dispatchable = ()
            deferred = tuple(
                item.timer_id
                for item in due
                if item.timer_id not in {firing.timer_id for firing in dispatchable}
            )
        else:
            dispatchable = tuple(
                self._pending.setdefault(item.timer_id, item) for item in due
            )
            deferred = ()
        self._deferred.update(deferred)
        self._deferred.difference_update(item.timer_id for item in dispatchable)
        self._last_poll_ms = as_of_ms
        return RuntimePoll(
            trade_date=self.trade_date,
            as_of_ms=as_of_ms,
            readiness=readiness,
            dispatchable_firings=dispatchable,
            deferred_timer_ids=deferred,
        )

    def acknowledge(self, firing: TimerFiring) -> None:
        """Commit one accepted timer dispatch in this in-memory session."""

        if not isinstance(firing, TimerFiring):
            raise TypeError("firing must be TimerFiring")
        if firing.session_plan_hash != self.session_plan.content_hash:
            raise ValueError("firing session plan does not match coordinator")
        known = self._fired.get(firing.timer_id)
        if known is not None:
            if known.content_hash == firing.content_hash:
                return
            raise ValueError("conflicting firing for completed timer")
        pending = self._pending.get(firing.timer_id)
        if pending is None:
            raise ValueError("timer firing was not returned by poll")
        if pending.content_hash != firing.content_hash:
            raise ValueError("firing does not match pending poll result")
        self._fired[firing.timer_id] = firing
        self._pending.pop(firing.timer_id, None)
        self._deferred.discard(firing.timer_id)
        self._frontier_ms = max(self._frontier_ms or firing.fired_time_ms, firing.fired_time_ms)

    def state_hash(self) -> str:
        """Return a deterministic diagnostic identity for current memory state."""

        return semantic_hash(
            {
                "contract_version": SESSION_RUNTIME_CONTRACT_VERSION,
                "trade_date": self.trade_date,
                "session_plan_hash": self.session_plan.content_hash,
                "frontier_ms": self._frontier_ms,
                "last_poll_ms": self._last_poll_ms,
                "fired": tuple(
                    (timer_id, firing.content_hash)
                    for timer_id, firing in sorted(self._fired.items())
                ),
                "pending": tuple(
                    (timer_id, firing.content_hash)
                    for timer_id, firing in sorted(self._pending.items())
                ),
                "deferred": tuple(sorted(self._deferred)),
            }
        )


__all__ = [
    "SESSION_RUNTIME_CONTRACT_VERSION",
    "RuntimePoll",
    "SessionRuntimeCoordinator",
]
