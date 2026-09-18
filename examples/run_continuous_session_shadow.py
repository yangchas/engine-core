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
    DeterministicEngine,
    EngineSignal,
    FrozenDataBundle,
    MarketStateReducer,
    OpeningShadowStrategy,
    Q2ProjectionSnapshot,
    SignalKind,
    WindowManager,
    WindowSpec,
    build_auction_reference_bundle,
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
_AUCTION_CUTOFFS = {
    "0920": time(9, 24),
    "0924": time(9, 25),
    "0925": time(9, 26),
}


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
    cutoff_ms: int,
    run_mode: str,
) -> None:
    """Reject cross-day or post-cutoff projections before they enter Engine."""

    if projection.trade_date != trade_date:
        raise ValueError(
            f"{node_id} projection trade_date does not match requested trade_date"
        )
    observed_ms = projection.envelope.observed_time_ms
    if run_mode == "NORMAL" and observed_ms > cutoff_ms:
        raise ValueError(f"{node_id} observed time is after node cutoff")
    source_times = tuple(
        value
        for value in (projection.oldest_source_time_ms, projection.newest_source_time_ms)
        if value is not None
    )
    if projection.status == DataStatus.MISSING:
        if source_times:
            raise ValueError(f"{node_id} missing projection contains source time")
        return
    if not source_times:
        raise ValueError(f"{node_id} projection is missing source time")
    if min(source_times) > max(source_times):
        raise ValueError(f"{node_id} source time range is reversed")
    if any(_source_date(value) != trade_date for value in source_times):
        raise ValueError(f"{node_id} source time crosses trade date")
    if run_mode == "NORMAL" and max(source_times) > cutoff_ms:
        raise ValueError(f"{node_id} source time is after node cutoff")


def _validate_session_inputs(
    *,
    auction_projections: Mapping[str, Q2ProjectionSnapshot],
    opening_projection: Q2ProjectionSnapshot,
    trade_date: str,
    symbol: str,
    preparation: AuctionReferencePreparation | None,
    run_mode: str,
) -> None:
    trade_date = _strict_trade_date(trade_date)
    symbol = _strict_symbol(symbol)
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
            cutoff_ms=_business_time_ms(trade_date, _AUCTION_CUTOFFS[tag]),
            run_mode=run_mode,
        )
        if symbol not in projection.expected_symbols:
            raise ValueError(f"AUCTION_{tag} projection does not contain symbol")
    _validate_projection_time(
        opening_projection,
        trade_date=trade_date,
        node_id="OPENING_0932",
        cutoff_ms=_business_time_ms(trade_date, time(9, 32)),
        run_mode=run_mode,
    )
    if preparation is not None:
        if preparation.trade_date != trade_date:
            raise ValueError("reference preparation trade_date does not match session")
        if preparation.knowledge_as_of_ms > _business_time_ms(trade_date, time(9, 25)):
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

    def __init__(self, *, symbol: str) -> None:
        self._auction = AuctionShadowStrategy(
            scope_id=symbol,
            start_trigger_id="AUCTION_0920",
            middle_trigger_id="AUCTION_0924",
            end_trigger_id="AUCTION_0925",
            previous_segment_id="auction_trial_" + symbol,
            current_segment_id="auction_reprice_" + symbol,
            previous_coverage_status="PARTIAL",
            current_coverage_status="PARTIAL",
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
    preparation: AuctionReferencePreparation | None = None,
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
        preparation=preparation,
        run_mode=run_mode,
    )


def run_continuous_redis_session_shadow(
    *,
    auction_projections: Sequence[Any],
    opening_projection: Q2ProjectionSnapshot,
    trade_date: str,
    symbol: str,
    preparation: AuctionReferencePreparation | None = None,
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
        preparation=preparation,
        run_mode=run_mode,
    )


def _run_projection_session(
    *,
    auction_projections: Mapping[str, Q2ProjectionSnapshot],
    opening_projection: Q2ProjectionSnapshot,
    trade_date: str,
    symbol: str,
    preparation: AuctionReferencePreparation | None,
    run_mode: str,
) -> dict[str, Any]:
    """Run already-adapted auction/Q2 projections in one Engine."""

    missing = [tag for tag in ANCHOR_ORDER if tag not in auction_projections]
    if missing:
        raise ValueError("missing auction projections: " + ",".join(missing))
    _validate_session_inputs(
        auction_projections=auction_projections,
        opening_projection=opening_projection,
        trade_date=trade_date,
        symbol=symbol,
        preparation=preparation,
        run_mode=run_mode,
    )
    strategy = ContinuousSessionShadowStrategy(symbol=symbol)
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("session", 0, 10**15),)),
        strategy,
        session_id=trade_date,
        phase="AUCTION_OPENING",
    )
    signal_seq = 1
    reference_bundle_hash = None
    for tag in ANCHOR_ORDER:
        projection = auction_projections[tag]
        logical_time_ms = _business_time_ms(trade_date, time(int(tag[:2]), int(tag[2:])))
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
        timer_payload: dict[str, Any] = {"trigger_id": f"AUCTION_{tag}"}
        if tag == "0925" and preparation is not None:
            timer_payload["data_requirements"] = AUCTION_REFERENCE_FUNCTION_ORDER
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
        if tag == "0925" and preparation is not None:
            pending = current.pending_evaluations
            if len(pending) != 1:
                raise RuntimeError("expected one pending 0925 evaluation")
            bundle = build_auction_reference_bundle(pending[0], preparation)
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

    opening_time_ms = _business_time_ms(trade_date, time(9, 32))
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
            {"trigger_id": "OPENING_0932"},
        )
    )
    current = engine.run_until_empty()
    # ``EngineRunResult.strategy_results`` is the bounded observable history,
    # not only the delta from this drain call.  Read it once after the final
    # stage so intermediate drains cannot duplicate entries in the report.
    result_history = list(current.strategy_results)
    final = result_history[-1]
    return {
        "contract_version": "ContinuousSessionShadowV1",
        "run_mode": run_mode,
        "trade_date": trade_date,
        "symbol": symbol,
        "single_engine": True,
        "processed_signals": current.processed_signals,
        "strategy_result_count": len(result_history),
        "reference_bundle_hash": reference_bundle_hash,
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
