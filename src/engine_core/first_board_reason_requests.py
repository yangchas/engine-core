"""Deterministic decision for fetching reasons for newly limit-up stocks."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .contracts import canonical_hash, deep_freeze


FIRST_BOARD_REASON_REQUESTS_CONTRACT_VERSION = "FirstBoardReasonRequestPlanV1"


@dataclass(frozen=True)
class ReasonRequestPlan:
    """A bounded, deterministic set of first-board reason fetch requests."""

    to_request: tuple[str, ...]
    skipped: Mapping[str, str]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        requests = tuple(self.to_request)
        skipped = {str(symbol): str(reason) for symbol, reason in self.skipped.items()}
        object.__setattr__(self, "to_request", requests)
        object.__setattr__(self, "skipped", deep_freeze(dict(sorted(skipped.items()))))
        object.__setattr__(
            self,
            "content_hash",
            canonical_hash(
                {
                    "contract_version": FIRST_BOARD_REASON_REQUESTS_CONTRACT_VERSION,
                    "to_request": requests,
                    "skipped": dict(sorted(skipped.items())),
                }
            ),
        )


def plan_first_board_reason_requests(
    *,
    trade_date: str,
    limit_state_by_symbol: Mapping[str, int | None],
    yest_pool_symbols: Collection[str],
    reasons_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    already_requested: Collection[str],
    max_requests: int = 50,
) -> ReasonRequestPlan:
    """Select first-board symbols that need an as-of reason fetch.

    Only symbols present in ``limit_state_by_symbol`` are evaluated. Missing
    states are not inferred; explicit ``None`` is reported as unknown.
    """

    if not isinstance(trade_date, str) or not trade_date:
        raise ValueError("trade_date is required")
    if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 0:
        raise ValueError("max_requests must be a non-negative integer")

    pool = set(yest_pool_symbols)
    requested = set(already_requested)
    eligible: list[str] = []
    skipped: dict[str, str] = {}

    for symbol in sorted(limit_state_by_symbol):
        state = limit_state_by_symbol[symbol]
        if state is None:
            skipped[symbol] = "STATE_UNKNOWN"
        elif state != 1:
            skipped[symbol] = "NOT_LIMIT_UP"
        elif symbol in pool:
            skipped[symbol] = "IN_POOL"
        elif any(
            isinstance(reason, Mapping) and reason.get("Date") == trade_date
            for reason in reasons_by_symbol.get(symbol, ())
        ):
            skipped[symbol] = "HAS_ASOF_REASON"
        elif symbol in requested:
            skipped[symbol] = "ALREADY_REQUESTED"
        else:
            eligible.append(symbol)

    to_request = tuple(eligible[:max_requests])
    for symbol in eligible[max_requests:]:
        skipped[symbol] = "OVER_BUDGET"

    return ReasonRequestPlan(to_request=to_request, skipped=skipped)
