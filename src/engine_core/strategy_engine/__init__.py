"""Composable, side-effect-free strategy components and simulated ledger."""
from .contracts import MarketSnapshot, RegimeDecision, StockCandidate, SelectionResult, OrderIntent, Position
from .regime import WeatherSoloRegime, Pos20Regime
from .selector import MainThemeNormalizedAccelSelector
from .portfolio import SyncSwitchPortfolioPolicy
from .ledger import SingleAccountLedger
from .engine import RegimeHistoryStore, StrategySpec, StrategyEngine, default_strategy_specs
