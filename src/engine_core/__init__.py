"""Independent deterministic market-state calculation core."""

from .clock import MonotonicClock, SystemMonotonicClock, SystemWallClock, VirtualClock, WallClock
from .contracts import (
    DataRequest,
    DataResult,
    DataStatus,
    EngineSignal,
    EngineSnapshot,
    FrozenDataBundle,
    MarketDataEnvelope,
    PayloadKind,
    SignalKind,
    canonical_hash,
)
from .engine import DeterministicEngine
from .probe import ProbeStrategy
from .q2 import Q2ProjectionSnapshot, RedisQ2ProjectionAdapter
from .state import CurrentMarketState, MarketStateReducer
from .windows import WindowManager, WindowSnapshot, WindowSpec

__all__ = [
    "CurrentMarketState",
    "DataRequest",
    "DataResult",
    "DataStatus",
    "DeterministicEngine",
    "EngineSignal",
    "EngineSnapshot",
    "FrozenDataBundle",
    "MarketDataEnvelope",
    "MarketStateReducer",
    "MonotonicClock",
    "PayloadKind",
    "ProbeStrategy",
    "Q2ProjectionSnapshot",
    "RedisQ2ProjectionAdapter",
    "SignalKind",
    "SystemMonotonicClock",
    "SystemWallClock",
    "VirtualClock",
    "WallClock",
    "WindowManager",
    "WindowSnapshot",
    "WindowSpec",
    "canonical_hash",
]
