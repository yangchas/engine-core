"""Run the smallest 09:20 -> 09:24 fact vertical slice."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (  # noqa: E402
    MarketStateReducer,
    RedisQ2ProjectionAdapter,
    build_segment_frame,
    compare_adjacent_segments,
)
from engine_core.contracts import canonical_json  # noqa: E402
from engine_core.windows import local_time_ms  # noqa: E402


class FakeRedis:
    def __init__(self, price: int, amount: int, bid: int, ask: int, ts: int):
        self._hash = {
            "px": str(price),
            "pc": "990",
            "amt": str(amount),
            "vol": "100",
            "br": str(bid),
            "ar": str(ask),
            "ts": str(ts),
        }

    def smembers(self, key: str):
        return {"000001"} if key == "q2:active:2026-09-04" else set()

    def hgetall(self, key: str):
        return dict(self._hash) if key == "q2:000001" else {}


def snapshot(
    price: int,
    amount: int,
    bid: int,
    ask: int,
    clock_text: str,
    trigger_id: str,
):
    source_time_ms = local_time_ms("2026-09-04", clock_text)
    observed_at = datetime.fromtimestamp(source_time_ms / 1000, timezone.utc)
    projection = RedisQ2ProjectionAdapter(
        FakeRedis(price, amount, bid, ask, source_time_ms)
    ).read("2026-09-04", observed_at)
    reducer = MarketStateReducer()
    reducer.apply_snapshot(
        projection,
        logical_time_ms=source_time_ms,
        session_id="2026-09-04",
        phase="AUCTION",
    )
    return reducer.build_snapshot(trigger_id, logical_time_ms=source_time_ms)


def main() -> None:
    at_0920 = snapshot(1020, 200, 30, 10, "09:20:00", "AUCTION_0920")
    at_0924 = snapshot(1010, 350, 25, 20, "09:24:00", "AUCTION_0924")
    trial = build_segment_frame(
        "auction_trial",
        snapshot(1000, 100, 20, 10, "09:19:00", "AUCTION_START"),
        at_0920,
        scope_type="SYMBOL",
        scope_id="000001",
        amount_semantics="CUMULATIVE",
        volume_semantics="CUMULATIVE",
    )
    reprice = build_segment_frame(
        "auction_reprice",
        at_0920,
        at_0924,
        scope_type="SYMBOL",
        scope_id="000001",
        amount_semantics="CUMULATIVE",
        volume_semantics="CUMULATIVE",
    )
    comparison = compare_adjacent_segments(trial, reprice)
    print(canonical_json({"trial": trial, "reprice": reprice, "comparison": comparison}))


if __name__ == "__main__":
    main()
