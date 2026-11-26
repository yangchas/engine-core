"""Contracts between independent strategy components."""
from dataclasses import dataclass
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class MarketSnapshot:
    trade_date: str
    prior_trade_date: str
    asof_ts: str
    regime_raw: Mapping[str, Any]
    themes: Any
    candidates: Any
    quotes_0945: Mapping[str, Any]
    eod_close: Any = None
    source_metadata: Any = None

    def __post_init__(self):
        if self.prior_trade_date >= self.trade_date or self.asof_ts[:10] != self.trade_date:
            raise ValueError('snapshot dates violate as-of ordering')


@dataclass(frozen=True)
class RegimeDecision:
    label: str
    target_exposure: float
    max_single_weight: float
    metrics: Mapping[str, Any]
    reasons: tuple

    def __post_init__(self):
        if self.label not in ('WARM_SOLO', 'COLD_SOLO', 'CROWDED', 'RED', 'WARM', 'COLD'):
            raise ValueError('invalid regime label')


@dataclass(frozen=True)
class StockCandidate:
    symbol: str
    name: str
    theme: str
    ret_25: float
    ret_45: float
    intraday_pos_45: float
    amt_45_yuan: float
    ask1: float
    av1: float
    prev_streak: int
    prev_broken: int


@dataclass(frozen=True)
class SelectionResult:
    themes: tuple
    picks: tuple
    funnel_counts: Mapping[str, int]


@dataclass(frozen=True)
class OrderIntent:
    side: str
    symbol: str
    target_weight: float = 0.0
    price: float | None = None
    reason: str = ''

    def __post_init__(self):
        if self.side not in ('BUY', 'SELL'):
            raise ValueError('invalid order side')


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    cost_basis: float
    buy_date: str
    batch_id: str


class RegimeDetector(Protocol):
    def detect(self, snapshot: MarketSnapshot, history=()) -> RegimeDecision: ...


class StockSelector(Protocol):
    def select(self, snapshot: MarketSnapshot) -> SelectionResult: ...


class PortfolioPolicy(Protocol):
    def plan(self, snapshot: MarketSnapshot, regime: RegimeDecision, selection: SelectionResult, positions) -> tuple[OrderIntent, ...]: ...
