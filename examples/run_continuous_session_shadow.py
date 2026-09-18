"""Run one bounded auction-to-opening shadow through one Engine instance.

This is deliberately a validation composition, not a new scheduler or a
production owner.  It proves that the already verified auction and opening
strategies can share one reducer, one window manager, and one deterministic
signal queue for a single symbol.  TD/Redis I/O stays outside this module;
callers pass already-read projections and rows.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from enum import Enum
from pathlib import Path
import sys
from collections.abc import Mapping as MappingABC
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (  # noqa: E402
    AUCTION_REFERENCE_FUNCTION_ORDER,
    AuctionReferencePreparation,
    AuctionShadowStrategy,
    DataStatus,
    DataResult,
    DeterministicEngine,
    EngineSignal,
    FrozenDataBundle,
    MarketStateReducer,
    OpeningShadowStrategy,
    Q2ProjectionSnapshot,
    SessionPlan,
    SessionRuntimeCoordinator,
    SignalKind,
    TimerSpec,
    TradingCalendarSnapshot,
    WindowManager,
    WindowSpec,
    semantic_hash,
)
from engine_core.contracts import StrategyResult  # noqa: E402

try:  # Script execution resolves sibling examples directly.
    from run_real_auction_engine_shadow import _projection_from_snapshot  # type: ignore
    from run_real_auction_shadow import ANCHOR_ORDER, build_snapshots_from_rows  # type: ignore
    from run_real_redis_auction_engine_shadow import build_engine_projection  # type: ignore
except ModuleNotFoundError:  # Pytest/import execution resolves the package.
    from examples.run_real_auction_engine_shadow import _projection_from_snapshot  # type: ignore
    from examples.run_real_auction_shadow import ANCHOR_ORDER, build_snapshots_from_rows  # type: ignore
    from examples.run_real_redis_auction_engine_shadow import build_engine_projection  # type: ignore


def _business_time_ms(trade_date: str, value: time) -> int:
    """Return an Asia/Shanghai business-anchor timestamp for signal ordering."""

    return int(
        datetime.combine(date.fromisoformat(trade_date), value).replace(
            tzinfo=ZoneInfo("Asia/Shanghai")
        ).timestamp()
        * 1000
    )


_RUN_MODES = {"NORMAL", "POSTMARKET_DIAGNOSTIC"}
_EVALUATION_KEYS = (*ANCHOR_ORDER, "OPENING_0932")


def _strict_trade_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("trade_date must be strict YYYY-MM-DD")
    return value


def _strict_symbol(value: str) -> str:
    if not isinstance(value, str) or len(value) != 6 or not value.isdigit():
        raise ValueError("symbol must be a six-digit code")
    return value


def _source_date(source_time_ms: int) -> str:
    return datetime.fromtimestamp(
        source_time_ms / 1000.0,
        tz=timezone.utc,
    ).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _validate_projection_time(
    projection: Q2ProjectionSnapshot,
    *,
    trade_date: str,
    node_id: str,
    evaluation_time_ms: int,
    run_mode: str,
) -> None:
    """Reject cross-day or post-cutoff projections before they enter Engine."""

    if projection.trade_date != trade_date:
        raise ValueError(
            f"{node_id} projection trade_date does not match requested trade_date"
        )
    observed_ms = projection.envelope.observed_time_ms
    if run_mode == "NORMAL" and observed_ms > evaluation_time_ms:
        raise ValueError(f"{node_id} observed time is after node cutoff")
    oldest_source = projection.oldest_source_time_ms
    newest_source = projection.newest_source_time_ms
    if (
        oldest_source is not None
        and newest_source is not None
        and oldest_source > newest_source
    ):
        raise ValueError(f"{node_id} source time range is reversed")
    source_times = tuple(
        value
        for value in (oldest_source, newest_source)
        if value is not None
    )
    if projection.status == DataStatus.MISSING:
        if source_times:
            raise ValueError(f"{node_id} missing projection contains source time")
        return
    if not source_times:
        raise ValueError(f"{node_id} projection is missing source time")
    if any(_source_date(value) != trade_date for value in source_times):
        raise ValueError(f"{node_id} source time crosses trade date")
    if run_mode == "NORMAL" and max(source_times) > evaluation_time_ms:
        raise ValueError(f"{node_id} source time is after node cutoff")


def _validate_evaluation_times(
    trade_date: str,
    evaluation_times_ms: Mapping[str, int] | None,
) -> dict[str, int]:
    trade_date = _strict_trade_date(trade_date)
    if evaluation_times_ms is None:
        raise ValueError(
            "evaluation_times_ms is required; pass the actual node firing times"
        )
    if set(evaluation_times_ms) != set(_EVALUATION_KEYS):
        raise ValueError(
            "evaluation_times_ms must contain exactly: "
            + ",".join(_EVALUATION_KEYS)
        )
    normalized: dict[str, int] = {}
    for key in _EVALUATION_KEYS:
        value = evaluation_times_ms[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"evaluation time for {key} must be a positive integer")
        local_date = datetime.fromtimestamp(
            value / 1000.0,
            tz=timezone.utc,
        ).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        if local_date != trade_date:
            raise ValueError(f"evaluation time for {key} crosses trade date")
        anchor_text = key[-4:]
        anchor_ms = _business_time_ms(
            trade_date,
            time(int(anchor_text[:2]), int(anchor_text[2:])),
        )
        if value < anchor_ms:
            raise ValueError(f"evaluation time for {key} is before business anchor")
        normalized[key] = value
    if any(
        normalized[left] >= normalized[right]
        for left, right in zip(_EVALUATION_KEYS, _EVALUATION_KEYS[1:])
    ):
        raise ValueError("evaluation times must be strictly increasing")
    return normalized


def _validate_session_inputs(
    *,
    auction_projections: Mapping[str, Q2ProjectionSnapshot],
    opening_projection: Q2ProjectionSnapshot,
    trade_date: str,
    symbol: str,
    preparation: AuctionReferencePreparation | None,
    evaluation_times_ms: Mapping[str, int] | None,
    run_mode: str,
) -> None:
    trade_date = _strict_trade_date(trade_date)
    symbol = _strict_symbol(symbol)
    evaluation_times = dict(evaluation_times_ms)
    if run_mode not in _RUN_MODES:
        raise ValueError("run_mode must be NORMAL or POSTMARKET_DIAGNOSTIC")
    if opening_projection.trade_date != trade_date:
        raise ValueError("opening projection trade_date does not match requested date")
    if symbol not in opening_projection.expected_symbols:
        raise ValueError("opening projection does not contain symbol")
    for tag in ANCHOR_ORDER:
        projection = auction_projections[tag]
        _validate_projection_time(
            projection,
            trade_date=trade_date,
            node_id=f"AUCTION_{tag}",
            evaluation_time_ms=evaluation_times[tag],
            run_mode=run_mode,
        )
        if symbol not in projection.expected_symbols:
            raise ValueError(f"AUCTION_{tag} projection does not contain symbol")
    _validate_projection_time(
        opening_projection,
        trade_date=trade_date,
        node_id="OPENING_0932",
        evaluation_time_ms=evaluation_times["OPENING_0932"],
        run_mode=run_mode,
    )
    if preparation is not None:
        if preparation.trade_date != trade_date:
            raise ValueError("reference preparation trade_date does not match session")
        if preparation.knowledge_as_of_ms > evaluation_times["0925"]:
            raise ValueError("reference preparation was observed after 0925 evaluation")


def _json_ready(value: Any) -> Any:
    """Convert frozen mappings/enums into deterministic JSON values."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, MappingABC):
        return {
            str(key): _json_ready(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_json_ready(item) for item in value]
    return value


class ContinuousSessionShadowStrategy:
    """Delegate auction and opening facts while retaining one session state."""

    strategy_id = "continuous-session-shadow-v1"

    def __init__(
        self,
        *,
        symbol: str,
        theme_delta_function_id: str | None = None,
    ) -> None:
        self._auction = AuctionShadowStrategy(
            scope_id=symbol,
            start_trigger_id="AUCTION_0920",
            middle_trigger_id="AUCTION_0924",
            end_trigger_id="AUCTION_0925",
            previous_segment_id="auction_trial_" + symbol,
            current_segment_id="auction_reprice_" + symbol,
            previous_coverage_status="PARTIAL",
            current_coverage_status="PARTIAL",
            theme_delta_function_id=theme_delta_function_id,
        )
        self._opening = OpeningShadowStrategy(scope_id=symbol)

    def evaluate(self, snapshot, bundle: FrozenDataBundle) -> StrategyResult:
        if snapshot.trigger_id in self._auction.required_trigger_ids:
            child = self._auction.evaluate(snapshot, bundle)
        elif snapshot.trigger_id == "OPENING_0932":
            child = self._opening.evaluate(snapshot, bundle)
        else:
            raise ValueError("unsupported continuous shadow trigger")
        trace = {
            "state": "OBSERVE",
            "decision_status": "FACT_ONLY",
            "strategy_id": self.strategy_id,
            "delegated_strategy_id": child.strategy_id,
            "trigger_id": snapshot.trigger_id,
            "child_trace": child.trace,
        }
        return StrategyResult(
            strategy_id=self.strategy_id,
            evaluation_id=bundle.evaluation_id,
            state="OBSERVE",
            trace=trace,
            evidence_refs=child.evidence_refs,
            content_hash=semantic_hash(trace),
        )


def run_continuous_session_shadow(
    *,
    auction_rows: Sequence[Sequence[Any] | Mapping[str, Any]],
    opening_projection: Q2ProjectionSnapshot,
    trade_date: str,
    symbol: str,
    calendar: TradingCalendarSnapshot,
    session_plan: SessionPlan,
    preparation: AuctionReferencePreparation | None = None,
    theme_delta_result: DataResult | None = None,
    evaluation_times_ms: Mapping[str, int] | None = None,
    run_mode: str = "NORMAL",
) -> dict[str, Any]:
    """Run 0920→0925→0932 using one Engine and already-read inputs.

    ``preparation`` must be a startup-frozen observation when provided.  No
    provider is called here, and no input is refreshed at a node boundary.
    """

    if not isinstance(opening_projection, Q2ProjectionSnapshot):
        raise TypeError("opening_projection must be Q2ProjectionSnapshot")
    _strict_trade_date(trade_date)
    _strict_symbol(symbol)
    snapshots = build_snapshots_from_rows(
        auction_rows,
        trade_date=trade_date,
        symbol=symbol,
    )
    auction_projections = {
        tag: _projection_from_snapshot(
            snapshots[tag],
            trade_date=trade_date,
            symbol=symbol,
        )
        for tag in ANCHOR_ORDER
    }
    return _run_projection_session(
        auction_projections=auction_projections,
        opening_projection=opening_projection,
        trade_date=trade_date,
        symbol=symbol,
        calendar=calendar,
        session_plan=session_plan,
        preparation=preparation,
        theme_delta_result=theme_delta_result,
        evaluation_times_ms=evaluation_times_ms,
        run_mode=run_mode,
    )


def run_continuous_redis_session_shadow(
    *,
    auction_projections: Sequence[Any],
    opening_projection: Q2ProjectionSnapshot,
    trade_date: str,
    symbol: str,
    calendar: TradingCalendarSnapshot,
    session_plan: SessionPlan,
    preparation: AuctionReferencePreparation | None = None,
    theme_delta_result: DataResult | None = None,
    evaluation_times_ms: Mapping[str, int] | None = None,
    run_mode: str = "NORMAL",
) -> dict[str, Any]:
    """Run Redis auction projections and Q2 through one Engine instance.

    The projections must already have been read by the caller with
    ``read_redis_auction_projection``.  This function never rereads Redis and
    never repairs a missing tag.
    """

    by_tag: dict[str, Any] = {}
    for projection in auction_projections:
        if projection.tag in by_tag:
            raise ValueError("duplicate Redis auction tag: " + str(projection.tag))
        by_tag[projection.tag] = projection
    missing = [tag for tag in ANCHOR_ORDER if tag not in by_tag]
    if missing:
        raise ValueError("missing Redis auction tags: " + ",".join(missing))
    converted = {
        tag: build_engine_projection(
            by_tag[tag],
            trade_date=trade_date,
            symbol=symbol,
        )
        for tag in ANCHOR_ORDER
    }
    return _run_projection_session(
        auction_projections=converted,
        opening_projection=opening_projection,
        trade_date=trade_date,
        symbol=symbol,
        calendar=calendar,
        session_plan=session_plan,
        preparation=preparation,
        theme_delta_result=theme_delta_result,
        evaluation_times_ms=evaluation_times_ms,
        run_mode=run_mode,
    )


def _run_projection_session(
    *,
    auction_projections: Mapping[str, Q2ProjectionSnapshot],
    opening_projection: Q2ProjectionSnapshot,
    trade_date: str,
    symbol: str,
    calendar: TradingCalendarSnapshot,
    session_plan: SessionPlan,
    preparation: AuctionReferencePreparation | None,
    theme_delta_result: DataResult | None,
    evaluation_times_ms: Mapping[str, int] | None,
    run_mode: str,
) -> dict[str, Any]:
    """Run already-adapted auction/Q2 projections in one Engine."""

    missing = [tag for tag in ANCHOR_ORDER if tag not in auction_projections]
    if missing:
        raise ValueError("missing auction projections: " + ",".join(missing))
    evaluation_times = _validate_evaluation_times(trade_date, evaluation_times_ms)
    if not isinstance(calendar, TradingCalendarSnapshot):
        raise TypeError("calendar must be TradingCalendarSnapshot")
    if not isinstance(session_plan, SessionPlan):
        raise TypeError("session_plan must be SessionPlan")
    _validate_session_inputs(
        auction_projections=auction_projections,
        opening_projection=opening_projection,
        trade_date=trade_date,
        symbol=symbol,
        preparation=preparation,
        evaluation_times_ms=evaluation_times,
        run_mode=run_mode,
    )
    theme_delta_function_id = _validate_theme_delta_result(
        theme_delta_result,
        trade_date=trade_date,
    )
    coordinator = SessionRuntimeCoordinator(
        trade_date=trade_date,
        calendar=calendar,
        session_plan=session_plan,
        timer_specs=(
            TimerSpec("AUCTION_0920", "09:20:00"),
            TimerSpec("AUCTION_0924", "09:24:00"),
            TimerSpec("AUCTION_0925", "09:25:00"),
            TimerSpec("OPENING_0932", "09:32:00"),
        ),
        q2_optional_timer_ids=(
            "AUCTION_0920",
            "AUCTION_0924",
            "AUCTION_0925",
        ),
    )
    strategy = ContinuousSessionShadowStrategy(
        symbol=symbol,
        theme_delta_function_id=theme_delta_function_id,
    )
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("session", 0, 10**15),)),
        strategy,
        session_id=trade_date,
        phase="AUCTION_OPENING",
    )
    signal_seq = 1
    reference_bundle_hash = None
    data_bundle_hash = None
    timer_firings: list[dict[str, Any]] = []
    for tag in ANCHOR_ORDER:
        projection = auction_projections[tag]
        logical_time_ms = evaluation_times[tag]
        business_anchor_ms = _business_time_ms(
            trade_date,
            time(int(tag[:2]), int(tag[2:])),
        )
        poll = coordinator.poll(
            as_of_ms=logical_time_ms,
            q2=None,
            origin="NORMAL",
        )
        firing = next(
            (
                item
                for item in poll.dispatchable_firings
                if item.timer_id == f"AUCTION_{tag}"
            ),
            None,
        )
        if firing is None:
            raise RuntimeError(f"coordinator did not dispatch AUCTION_{tag}")
        # A coordinator poll can discover multiple overdue timers at once.
        # ``fired_time_ms`` is then the first dispatch observation shared by
        # each pending TimerFiring, while ``logical_time_ms`` is this node's
        # explicit Engine evaluation/cutoff time.  They must not be conflated:
        # a pending firing may be older, but can never be from the future.
        if firing.fired_time_ms > logical_time_ms:
            raise RuntimeError(f"coordinator firing time is after evaluation for AUCTION_{tag}")
        timer_firings.append(
            {
                "timer_id": firing.timer_id,
                "scheduled_time_ms": firing.scheduled_time_ms,
                "fired_time_ms": firing.fired_time_ms,
                "origin": firing.origin,
                "content_hash": firing.content_hash,
            }
        )
        engine.submit(
            EngineSignal(
                f"continuous-market-{tag}",
                logical_time_ms,
                signal_seq,
                SignalKind.MARKET_UPDATE,
                projection,
            )
        )
        signal_seq += 1
        timer_payload: dict[str, Any] = {
            "trigger_id": f"AUCTION_{tag}",
            "business_anchor_time_ms": business_anchor_ms,
            "dispatch_fired_time_ms": firing.fired_time_ms,
            "evaluation_time_ms": logical_time_ms,
            "firing_time_ms": logical_time_ms,
            "timer_firing_content_hash": firing.content_hash,
        }
        if tag == "0925" and (
            preparation is not None or theme_delta_result is not None
        ):
            timer_payload["data_requirements"] = _continuous_reference_order(
                theme_delta_function_id,
                include_references=preparation is not None,
            )
        engine.submit(
            EngineSignal(
                f"continuous-timer-{tag}",
                logical_time_ms,
                signal_seq,
                SignalKind.TIMER,
                timer_payload,
            )
        )
        signal_seq += 1
        current = engine.run_until_empty()
        if tag == "0925" and (
            preparation is not None or theme_delta_result is not None
        ):
            pending = current.pending_evaluations
            if len(pending) != 1:
                raise RuntimeError("expected one pending 0925 evaluation")
            bundle = _build_continuous_bundle(
                pending[0],
                preparation=preparation,
                theme_delta_result=theme_delta_result,
                theme_delta_function_id=theme_delta_function_id,
            )
            data_bundle_hash = bundle.content_hash
            if theme_delta_result is None:
                reference_bundle_hash = bundle.content_hash
            engine.submit(
                EngineSignal(
                    "continuous-data-ready-0925",
                    logical_time_ms + 1,
                    signal_seq,
                    SignalKind.DATA_READY,
                    {"evaluation_id": pending[0].evaluation_id, "bundle": bundle},
                )
            )
            signal_seq += 1
            current = engine.run_until_empty()
        coordinator.acknowledge(firing)

    opening_time_ms = evaluation_times["OPENING_0932"]
    opening_business_anchor_ms = _business_time_ms(trade_date, time(9, 32))
    opening_poll = coordinator.poll(
        as_of_ms=opening_time_ms,
        q2=opening_projection,
        origin="NORMAL",
    )
    opening_firing = next(
        (
            item
            for item in opening_poll.dispatchable_firings
            if item.timer_id == "OPENING_0932"
        ),
        None,
    )
    if opening_firing is None:
        raise RuntimeError("coordinator did not dispatch OPENING_0932")
    if opening_firing.fired_time_ms > opening_time_ms:
        raise RuntimeError("coordinator firing time is after evaluation for OPENING_0932")
    timer_firings.append(
        {
            "timer_id": opening_firing.timer_id,
            "scheduled_time_ms": opening_firing.scheduled_time_ms,
            "fired_time_ms": opening_firing.fired_time_ms,
            "origin": opening_firing.origin,
            "content_hash": opening_firing.content_hash,
        }
    )
    engine.submit(
        EngineSignal(
            "continuous-market-opening-0932",
            opening_time_ms,
            signal_seq,
            SignalKind.MARKET_UPDATE,
            opening_projection,
        )
    )
    signal_seq += 1
    engine.submit(
        EngineSignal(
            "continuous-timer-opening-0932",
            opening_time_ms,
            signal_seq,
            SignalKind.TIMER,
        {
            "trigger_id": "OPENING_0932",
            "business_anchor_time_ms": opening_business_anchor_ms,
            "dispatch_fired_time_ms": opening_firing.fired_time_ms,
            "evaluation_time_ms": opening_time_ms,
            "firing_time_ms": opening_time_ms,
            "timer_firing_content_hash": opening_firing.content_hash,
        },
        )
    )
    current = engine.run_until_empty()
    coordinator.acknowledge(opening_firing)
    # ``EngineRunResult.strategy_results`` is the bounded observable history,
    # not only the delta from this drain call.  Read it once after the final
    # stage so intermediate drains cannot duplicate entries in the report.
    result_history = list(current.strategy_results)
    final = result_history[-1]
    return {
        "contract_version": "ContinuousSessionShadowV1",
        "run_mode": run_mode,
        "evaluation_times_ms": dict(evaluation_times),
        "trade_date": trade_date,
        "symbol": symbol,
        "single_engine": True,
        "coordinator": {
            "session_plan_hash": session_plan.content_hash,
            "state_hash": coordinator.state_hash(),
            "completed_timer_ids": coordinator.completed_timer_ids,
            "timer_firings": tuple(timer_firings),
        },
        "processed_signals": current.processed_signals,
        "strategy_result_count": len(result_history),
        "reference_bundle_hash": reference_bundle_hash,
        "data_bundle_hash": data_bundle_hash,
        "opening_status": final.trace["child_trace"].get("fact_status"),
        "strategy_results": tuple(_json_ready(result.trace) for result in result_history),
        "pending_evaluations": tuple(
            {
                "evaluation_id": item.evaluation_id,
                "trigger_id": item.trigger_id,
                "knowledge_as_of_ms": item.knowledge_as_of_ms,
                "function_order": item.function_order,
                "snapshot_content_hash": item.snapshot_content_hash,
            }
            for item in current.pending_evaluations
        ),
        "read_only": True,
        "side_effect_boundary": "already-read projections + in-memory Engine only",
    }


__all__ = [
    "ContinuousSessionShadowStrategy",
    "run_continuous_session_shadow",
    "run_continuous_redis_session_shadow",
]


_THEME_DELTA_FUNCTION_ID = "theme_auction_delta_compat"


def _validate_theme_delta_result(
    result: DataResult | None,
    *,
    trade_date: str,
) -> str | None:
    """Validate one already-computed compatibility fact input.

    The continuous runner accepts a frozen ``DataResult`` only; it never
    performs theme aggregation, mapping lookup, Redis access, or fallback.
    ``UNAVAILABLE``/``MISSING`` results remain diagnostic and are still bound
    to the evaluation so the strategy can report the missing state truthfully.
    """

    if result is None:
        return None
    if not isinstance(result, DataResult):
        raise TypeError("theme_delta_result must be DataResult")
    if result.function_id != _THEME_DELTA_FUNCTION_ID:
        raise ValueError(
            "theme_delta_result function_id must be " + _THEME_DELTA_FUNCTION_ID
        )
    if result.requested_trade_date != trade_date:
        raise ValueError("theme_delta_result requested_trade_date does not match session")
    if result.actual_trade_date not in (None, trade_date):
        raise ValueError("theme_delta_result actual_trade_date does not match session")
    return result.function_id


def _continuous_reference_order(
    theme_delta_function_id: str | None,
    *,
    include_references: bool,
) -> tuple[str, ...]:
    """Return the single 0925 requirement order for this run."""

    order = AUCTION_REFERENCE_FUNCTION_ORDER if include_references else ()
    if theme_delta_function_id is None:
        return order
    if theme_delta_function_id in order:
        raise ValueError("theme delta function collides with auction reference order")
    return (*order, theme_delta_function_id)


def _build_continuous_bundle(
    pending: Any,
    *,
    preparation: AuctionReferencePreparation | None,
    theme_delta_result: DataResult | None,
    theme_delta_function_id: str | None,
) -> FrozenDataBundle:
    """Bind existing reference and optional theme results once to one evaluation."""

    expected_order = _continuous_reference_order(
        theme_delta_function_id,
        include_references=preparation is not None,
    )
    if tuple(pending.function_order) != expected_order:
        raise ValueError("0925 pending function order does not match supplied inputs")
    results: dict[str, DataResult] = {}
    if preparation is not None:
        if tuple(preparation.results[i][0] for i in range(len(preparation.results))) != AUCTION_REFERENCE_FUNCTION_ORDER:
            raise ValueError("auction reference preparation order is invalid")
        results.update(preparation.as_mapping())
    if theme_delta_result is not None:
        results[theme_delta_result.function_id] = theme_delta_result
    if set(results) != set(expected_order):
        missing = sorted(set(expected_order).difference(results))
        extra = sorted(set(results).difference(expected_order))
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if extra:
            detail.append("unexpected=" + ",".join(extra))
        raise ValueError("0925 bundle inputs do not match requirements: " + "; ".join(detail))
    return FrozenDataBundle.from_results(
        evaluation_id=pending.evaluation_id,
        knowledge_as_of_ms=pending.knowledge_as_of_ms,
        function_order=expected_order,
        results_by_function=results,
    )
