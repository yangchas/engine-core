"""Minimal, explicit reference-data preparation for auction evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Tuple

from .calendar import TradingCalendarSnapshot, parse_trade_date
from .contracts import DataRequest, DataResult, FrozenDataBundle, semantic_hash
from .data import DataContext, PreviousDayStatsFunction, build_frozen_bundle
from .engine import PendingEvaluationRequest
from .hot_plates import HotPlatesFunction
from .previous_day_limit_pool import PreviousDayLimitPoolFunction


AUCTION_REFERENCE_CONTRACT_VERSION = "AuctionReferencePreparationV1"
AUCTION_REFERENCE_FUNCTION_ORDER = (
    "previous_day_stats",
    "previous_day_limit_pool",
    "hot_plates",
)


@dataclass(frozen=True)
class AuctionReferencePreparation:
    """Frozen output of one bounded auction-reference preparation."""

    trade_date: str
    previous_trade_date: str
    knowledge_as_of_ms: int
    results: Tuple[Tuple[str, DataResult], ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        ordered = tuple(self.results)
        if tuple(function_id for function_id, _ in ordered) != AUCTION_REFERENCE_FUNCTION_ORDER:
            raise ValueError("auction reference results must follow the fixed function order")
        if any(
            not isinstance(result, DataResult) or result.function_id != function_id
            for function_id, result in ordered
        ):
            raise ValueError("auction reference result identity does not match its function")
        object.__setattr__(self, "results", ordered)
        object.__setattr__(
            self,
            "content_hash",
            semantic_hash(
                {
                    "contract_version": AUCTION_REFERENCE_CONTRACT_VERSION,
                    "trade_date": self.trade_date,
                    "previous_trade_date": self.previous_trade_date,
                    "knowledge_as_of_ms": self.knowledge_as_of_ms,
                    "results": tuple(
                        (function_id, result.content_hash)
                        for function_id, result in ordered
                    ),
                }
            ),
        )

    def as_mapping(self) -> Mapping[str, DataResult]:
        """Return a fresh lookup mapping without exposing mutable identity."""

        return dict(self.results)


def build_auction_reference_bundle(
    pending: PendingEvaluationRequest,
    preparation: AuctionReferencePreparation,
) -> FrozenDataBundle:
    """Bind prepared references to one Engine-owned frozen evaluation.

    Preparation performs provider I/O outside the reducer.  This function is
    the small deterministic hand-off back to Engine: it accepts only the
    fixed auction requirement order and a preparation cutoff that is no later
    than the knowledge cutoff captured when the Engine froze its snapshot.
    """

    if not isinstance(pending, PendingEvaluationRequest):
        raise TypeError("pending must be a PendingEvaluationRequest")
    if not isinstance(preparation, AuctionReferencePreparation):
        raise TypeError("preparation must be an AuctionReferencePreparation")
    if pending.function_order != AUCTION_REFERENCE_FUNCTION_ORDER:
        raise ValueError("pending evaluation does not request auction references")
    if preparation.knowledge_as_of_ms > pending.knowledge_as_of_ms:
        raise ValueError("reference preparation is after evaluation cutoff")
    return build_frozen_bundle(
        evaluation_id=pending.evaluation_id,
        knowledge_as_of_ms=pending.knowledge_as_of_ms,
        function_order=pending.function_order,
        results_by_function=preparation.as_mapping(),
    )


def prepare_auction_references(
    *,
    trade_date: str,
    knowledge_as_of_ms: int,
    context: DataContext,
    calendar: TradingCalendarSnapshot,
    previous_day_stats: PreviousDayStatsFunction,
    previous_day_limit_pool: PreviousDayLimitPoolFunction,
    hot_plates: HotPlatesFunction,
    symbols: Tuple[str, ...] = (),
) -> AuctionReferencePreparation:
    """Run the current three auction reference functions in fixed order.

    Providers may block, so this belongs before a node or in a worker, never
    inside the Engine reducer.  It does not retry, fall back, repair caches,
    or change source semantics.
    """

    if not isinstance(calendar, TradingCalendarSnapshot):
        raise TypeError("calendar must be a TradingCalendarSnapshot")
    parsed = parse_trade_date(trade_date)
    if not calendar.is_trading_day(parsed):
        raise ValueError("auction reference trade_date is not a trading day")
    if isinstance(knowledge_as_of_ms, bool) or not isinstance(knowledge_as_of_ms, int):
        raise TypeError("knowledge_as_of_ms must be an integer")
    if knowledge_as_of_ms <= 0:
        raise ValueError("knowledge_as_of_ms must be positive")
    if not isinstance(context, DataContext):
        raise TypeError("context must be a DataContext")
    normalized_symbols = tuple(sorted(set(symbols)))
    previous_trade_date = calendar.previous_trade_day(parsed).isoformat()

    def request(function_id: str, *, request_symbols: Tuple[str, ...] = ()) -> DataRequest:
        return DataRequest(
            request_id=(
                "auction-reference:"
                + trade_date
                + ":"
                + str(knowledge_as_of_ms)
                + ":"
                + function_id
            ),
            function_id=function_id,
            trade_date=trade_date,
            effective_as_of_ms=knowledge_as_of_ms,
            knowledge_as_of_ms=knowledge_as_of_ms,
            symbols=request_symbols,
            purpose="auction_reference_prefetch",
            temporal_mode=context.temporal_mode,
        )

    ordered = (
        (
            "previous_day_stats",
            previous_day_stats.execute(
                context,
                request("previous_day_stats", request_symbols=normalized_symbols),
            ),
        ),
        (
            "previous_day_limit_pool",
            previous_day_limit_pool.execute(
                context,
                request("previous_day_limit_pool"),
            ),
        ),
        (
            "hot_plates",
            hot_plates.execute(context, request("hot_plates")),
        ),
    )
    return AuctionReferencePreparation(
        trade_date=trade_date,
        previous_trade_date=previous_trade_date,
        knowledge_as_of_ms=knowledge_as_of_ms,
        results=ordered,
    )
