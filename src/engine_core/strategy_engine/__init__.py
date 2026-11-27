"""Composable, side-effect-free strategy components and simulated ledger."""
from .contracts import MarketSnapshot, RegimeDecision, StockCandidate, SelectionResult, OrderIntent, Position
from .regime import WeatherSoloRegime, Pos20Regime, MacroB1Regime, AlwaysOnBaselineRegime, compute_macro_b1_series
from .selector import MainThemeNormalizedAccelSelector, LowPos20SectorSelector
from .portfolio import SyncSwitchPortfolioPolicy
from .ledger import SingleAccountLedger
from .engine import RegimeHistoryStore, StrategySpec, StrategyEngine, default_strategy_specs, all_strategy_specs
