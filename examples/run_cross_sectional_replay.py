"""Offline demonstration of one Engine receiving one update per market frame.

The example uses in-memory rows only.  It deliberately does not connect to
Redis, TDengine, RabbitMQ or any production recovery owner.
"""

from __future__ import annotations

from datetime import datetime, timezone

from engine_core import (
    CrossSectionReplaySource,
    DeterministicEngine,
    MarketStateReducer,
    ProbeStrategy,
    VirtualClock,
    WindowManager,
    WindowSpec,
    local_datetime_ms,
)


def main() -> None:
    start = local_datetime_ms("2026-09-18", "09:15:00")
    end = local_datetime_ms("2026-09-18", "09:40:00")
    source = CrossSectionReplaySource(
        "2026-09-18",
        ("000001", "600519"),
        VirtualClock(datetime.fromtimestamp(start / 1000, timezone.utc)),
        slice_anchor_ms=start,
        end_exclusive_ms=end,
    )
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("intraday", 0, 10**15),)),
        ProbeStrategy(),
        session_id="2026-09-18-cross-section-replay",
        phase="REPLAY",
    )
    rows = [
        {
            "ts": start + 250,
            "symbol": "600519",
            "px_milli": 1_000_000,
            "pc_milli": 999_000,
            "amt_yuan": 100,
            "vol_units": 1,
        }
    ]
    source.replay(rows, engine)
    print({
        "frames": source.frame_count,
        "processed_signals": engine._processed,
        "last_revision": engine._reducer.state.revision,
        "side_effects": "NONE",
    })


if __name__ == "__main__":
    main()
