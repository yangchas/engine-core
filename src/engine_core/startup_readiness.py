"""Small, side-effect-free startup readiness assessment.

The assessment is deliberately not a coordinator or workflow.  It validates
the explicit calendar/session identity, reports the current Q2 quality, checks
already-observed reference results, and returns due timer identities for the
caller to dispatch.  It never reads a data source, performs a prefetch, or
writes state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Tuple

from .calendar import TradingCalendarSnapshot, parse_trade_date
from .contracts import DataResult, DataStatus, semantic_hash
from .data import TemporalDataGuard
from .q2 import Q2ProjectionSnapshot
from .session import SessionPlan
from .timers import TimerFiring, TimerSpec, due_timer_firings


STARTUP_READINESS_CONTRACT_VERSION = "StartupReadinessV1"


@dataclass(frozen=True)
class StartupReadiness:
    """Immutable result of one explicit startup/self-check evaluation.

    ``status`` is an operational summary, not a replacement for Q2 or
    ``DataStatus``: ``READY`` means the inputs are usable, ``PARTIAL`` means
    the engine may continue with explicit degradation, and ``BLOCKED`` means
    the caller must not dispatch data-dependent work.
    """

    trade_date: str
    as_of_ms: int
    phase: str
    status: str
    calendar_semantic_hash: str
    session_plan_hash: str
    q2_status: str
    q2_consistency_status: str
    q2_content_hash: Optional[str]
    q2_coverage: float
    q2_oldest_source_time_ms: Optional[int]
    q2_newest_source_time_ms: Optional[int]
    reference_statuses: Tuple[Tuple[str, str], ...]
    reference_content_hashes: Tuple[Tuple[str, Optional[str]], ...]
    due_timer_ids: Tuple[str, ...]
    actions: Tuple[str, ...]
    reasons: Tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.status not in {"READY", "PARTIAL", "BLOCKED"}:
            raise ValueError("startup readiness status is invalid")
        if isinstance(self.as_of_ms, bool) or not isinstance(self.as_of_ms, int):
            raise TypeError("as_of_ms must be an integer")
        if self.as_of_ms <= 0:
            raise ValueError("as_of_ms must be positive")
        if not 0.0 <= self.q2_coverage <= 1.0:
            raise ValueError("q2_coverage must be between 0 and 1")
        object.__setattr__(self, "reference_statuses", tuple(self.reference_statuses))
        object.__setattr__(
            self,
            "reference_content_hashes",
            tuple(self.reference_content_hashes),
        )
        object.__setattr__(self, "due_timer_ids", tuple(self.due_timer_ids))
        object.__setattr__(self, "actions", tuple(self.actions))
        object.__setattr__(self, "reasons", tuple(self.reasons))
        object.__setattr__(
            self,
            "content_hash",
            semantic_hash(
                {
                    "contract_version": STARTUP_READINESS_CONTRACT_VERSION,
                    "trade_date": self.trade_date,
                    "as_of_ms": self.as_of_ms,
                    "phase": self.phase,
                    "status": self.status,
                    "calendar_semantic_hash": self.calendar_semantic_hash,
                    "session_plan_hash": self.session_plan_hash,
                    "q2_status": self.q2_status,
                    "q2_consistency_status": self.q2_consistency_status,
                    "q2_content_hash": self.q2_content_hash,
                    "q2_coverage": self.q2_coverage,
                    "q2_oldest_source_time_ms": self.q2_oldest_source_time_ms,
                    "q2_newest_source_time_ms": self.q2_newest_source_time_ms,
                    "reference_statuses": self.reference_statuses,
                    "reference_content_hashes": self.reference_content_hashes,
                    "due_timer_ids": self.due_timer_ids,
                    "actions": self.actions,
                    "reasons": self.reasons,
                }
            ),
        )


def assess_startup_readiness(
    trade_date: str,
    as_of_ms: int,
    calendar: TradingCalendarSnapshot,
    session_plan: SessionPlan,
    *,
    q2: Optional[Q2ProjectionSnapshot],
    required_reference_functions: Iterable[str] = (),
    reference_results: Optional[Mapping[str, DataResult]] = None,
    timer_specs: Iterable[TimerSpec] = (),
    previous_time_ms: Optional[int] = None,
    already_fired: Iterable[str] = (),
    origin: str = "NORMAL",
) -> StartupReadiness:
    """Assess one startup point without performing any I/O.

    ``required_reference_functions`` is an ordered declaration.  Every
    declared function must have a corresponding already-guarded
    :class:`DataResult` in ``reference_results``.  Missing or cutoff-unsafe
    results produce ``PARTIAL``/``BLOCKED`` and a prefetch action; this
    function never invokes a provider itself.

    ``as_of_ms`` is the logical startup instant.  Timer due-ness is delegated
    to the existing pure ``due_timer_firings`` wheel.  A caller that is
    recovering after a cold start should pass ``previous_time_ms=None`` and
    ``origin='RECOVERY_CATCHUP'``.
    """

    if not isinstance(calendar, TradingCalendarSnapshot):
        raise TypeError("calendar must be a TradingCalendarSnapshot")
    if not isinstance(session_plan, SessionPlan):
        raise TypeError("session_plan must be a SessionPlan")
    if isinstance(as_of_ms, bool) or not isinstance(as_of_ms, int):
        raise TypeError("as_of_ms must be an integer")
    if as_of_ms <= 0:
        raise ValueError("as_of_ms must be positive")
    parsed_trade_date = parse_trade_date(trade_date)
    if parsed_trade_date.isoformat() != trade_date:
        raise ValueError("trade_date must be strict YYYY-MM-DD")
    if session_plan.trade_date != trade_date:
        raise ValueError("session plan trade_date does not match trade_date")
    if (
        session_plan.calendar_id != calendar.calendar_id
        or session_plan.calendar_version != calendar.version
        or session_plan.calendar_semantic_hash != calendar.semantic_hash
    ):
        raise ValueError("session plan calendar identity does not match calendar")
    if not calendar.is_trading_day(trade_date):
        raise ValueError("startup trade_date is not a trading day")
    phase = session_plan.phase_at_ms(as_of_ms)

    declared_functions = tuple(required_reference_functions)
    if any(not isinstance(item, str) or not item for item in declared_functions):
        raise ValueError("required_reference_functions must contain non-empty ids")
    if len(declared_functions) != len(set(declared_functions)):
        raise ValueError("required_reference_functions must be unique")
    supplied_results = reference_results or {}
    if not isinstance(supplied_results, Mapping):
        raise TypeError("reference_results must be a mapping")

    reasons: list[str] = []
    actions: list[str] = []
    status = "READY"

    if q2 is None:
        q2_status = DataStatus.MISSING.value
        q2_consistency = "NO_Q2"
        q2_content_hash = None
        q2_coverage = 0.0
        q2_oldest = None
        q2_newest = None
        status = "BLOCKED"
        reasons.append("q2_missing")
        actions.append("WAIT_FOR_Q2")
    else:
        if q2.trade_date != trade_date:
            raise ValueError("q2 trade_date does not match trade_date")
        q2_status = q2.status.value
        q2_consistency = q2.consistency_status
        q2_content_hash = q2.content_hash
        q2_coverage = q2.coverage
        q2_oldest = q2.oldest_source_time_ms
        q2_newest = q2.newest_source_time_ms
        if (
            q2.envelope.observed_time_ms > as_of_ms
            or (
                q2.newest_source_time_ms is not None
                and q2.newest_source_time_ms > as_of_ms
            )
        ):
            q2_status = DataStatus.UNAVAILABLE.value
            status = "BLOCKED"
            reasons.append("q2_observation_after_cutoff")
            actions.append("WAIT_FOR_Q2")
        elif q2.status in {
            DataStatus.MISSING,
            DataStatus.INVALID,
            DataStatus.ERROR,
            DataStatus.UNAVAILABLE,
        }:
            status = "BLOCKED"
            reasons.append("q2_status:" + q2.status.value)
            actions.append("WAIT_FOR_Q2")
        elif q2.status in {DataStatus.PARTIAL, DataStatus.STALE}:
            status = "PARTIAL"
            reasons.append("q2_status:" + q2.status.value)
            actions.append("REFRESH_Q2")

    reference_statuses = []
    reference_content_hashes = []
    for function_id in declared_functions:
        result = supplied_results.get(function_id)
        if result is None:
            reference_status = "MISSING"
            reference_content_hash = None
            reasons.append("reference_missing:" + function_id)
            actions.append("PREFETCH:" + function_id)
            if status == "READY":
                status = "PARTIAL"
        elif not isinstance(result, DataResult):
            raise TypeError("reference_results values must be DataResult")
        else:
            reference_status = result.status.value
            reference_content_hash = result.content_hash
            if result.function_id != function_id:
                raise ValueError("reference result function_id does not match key")
            if result.requested_trade_date != trade_date:
                reference_status = "INVALID"
                reasons.append("reference_trade_date:" + function_id)
                actions.append("PREFETCH:" + function_id)
            else:
                # Re-run the guard at the startup cutoff.  In particular,
                # unknown available_at is not treated as infinitely old.
                guarded = TemporalDataGuard.check(
                    result,
                    _readiness_request(result, trade_date, as_of_ms),
                )
                reference_status = guarded.status.value
                if guarded.status is not DataStatus.READY:
                    reasons.append("reference_status:" + function_id + ":" + guarded.status.value)
                    actions.append("PREFETCH:" + function_id)
                else:
                    reference_content_hash = guarded.content_hash
        if reference_status not in {DataStatus.READY.value} and status == "READY":
            status = "PARTIAL"
        reference_statuses.append((function_id, reference_status))
        reference_content_hashes.append((function_id, reference_content_hash))

    firings: Tuple[TimerFiring, ...] = due_timer_firings(
        session_plan,
        tuple(timer_specs),
        previous_time_ms=previous_time_ms,
        current_time_ms=as_of_ms,
        already_fired=tuple(already_fired),
        origin=origin,
    )
    due_timer_ids = tuple(item.timer_id for item in firings)
    timer_action = "DISPATCH_TIMER:" if status != "BLOCKED" else "DEFER_TIMER:"
    actions.extend(timer_action + timer_id for timer_id in due_timer_ids)

    return StartupReadiness(
        trade_date=trade_date,
        as_of_ms=as_of_ms,
        phase=phase,
        status=status,
        calendar_semantic_hash=calendar.semantic_hash,
        session_plan_hash=session_plan.content_hash,
        q2_status=q2_status,
        q2_consistency_status=q2_consistency,
        q2_content_hash=q2_content_hash,
        q2_coverage=q2_coverage,
        q2_oldest_source_time_ms=q2_oldest,
        q2_newest_source_time_ms=q2_newest,
        reference_statuses=tuple(reference_statuses),
        reference_content_hashes=tuple(reference_content_hashes),
        due_timer_ids=due_timer_ids,
        actions=tuple(actions),
        reasons=tuple(reasons),
    )


def _readiness_request(
    result: DataResult,
    trade_date: str,
    as_of_ms: int,
):
    """Build the smallest request needed to reapply the temporal guard."""

    # Import locally to keep the module's public surface focused on the
    # readiness assessment while reusing the existing request contract.
    from .contracts import DataRequest

    return DataRequest(
        request_id=result.request_id,
        function_id=result.function_id,
        trade_date=trade_date,
        effective_as_of_ms=as_of_ms,
        knowledge_as_of_ms=as_of_ms,
        temporal_mode=result.temporal_mode,
    )
