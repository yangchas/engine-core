"""Run one bounded auction-to-opening shadow through one Engine instance.

This is deliberately a validation composition, not a new scheduler or a
production owner.  It proves that the already verified auction and opening
strategies can share one reducer, one window manager, and one deterministic
signal queue for a single symbol.  TD/Redis I/O stays outside this module;
callers pass already-read projections and rows.
"""

from __future__ import annotations

from datetime import date, datetime, time
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
except ModuleNotFoundError:  # Pytest/import execution resolves the package.
    from examples.run_real_auction_engine_shadow import _projection_from_snapshot  # type: ignore
    from examples.run_real_auction_shadow import ANCHOR_ORDER, build_snapshots_from_rows  # type: ignore


def _business_time_ms(trade_date: str, value: time) -> int:
    """Return an Asia/Shanghai business-anchor timestamp for signal ordering."""

    return int(
        datetime.combine(date.fromisoformat(trade_date), value).replace(
            tzinfo=ZoneInfo("Asia/Shanghai")
        ).timestamp()
        * 1000
    )


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
) -> dict[str, Any]:
    """Run 0920→0925→0932 using one Engine and already-read inputs.

    ``preparation`` must be a startup-frozen observation when provided.  No
    provider is called here, and no input is refreshed at a node boundary.
    """

    if not isinstance(opening_projection, Q2ProjectionSnapshot):
        raise TypeError("opening_projection must be Q2ProjectionSnapshot")
    if symbol not in opening_projection.expected_symbols:
        raise ValueError("opening projection does not contain symbol")
    snapshots = build_snapshots_from_rows(
        auction_rows,
        trade_date=trade_date,
        symbol=symbol,
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
        snapshot = snapshots[tag]
        projection = _projection_from_snapshot(
            snapshot,
            trade_date=trade_date,
            symbol=symbol,
        )
        logical_time_ms = snapshot.logical_time_ms
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
        timer_payload: dict[str, Any] = {"trigger_id": snapshot.trigger_id}
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


__all__ = ["ContinuousSessionShadowStrategy", "run_continuous_session_shadow"]
