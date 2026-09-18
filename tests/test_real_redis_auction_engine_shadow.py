import importlib.util
import json
from pathlib import Path

from engine_core import canonical_json, read_redis_auction_projection


SPEC = importlib.util.spec_from_file_location(
    "real_redis_auction_engine_shadow",
    Path(__file__).parents[1]
    / "examples"
    / "run_real_redis_auction_engine_shadow.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class FakeRedis:
    def __init__(self, rows_by_tag):
        self.rows_by_tag = rows_by_tag
        self.calls = []

    def hgetall(self, key):
        self.calls.append(key)
        tag = key[-4:]
        rows = self.rows_by_tag.get(tag, ())
        ts = {"0920": 1789348803146, "0924": 1789349040110, "0925": 1789349106810}[tag]
        return {
            "meta": json.dumps({"tag": tag, "ts": ts, "n": len(rows)}),
            "summary": json.dumps({"tag": tag, "ts": ts}),
            "top_amount": json.dumps(list(rows)),
        }


def _row(symbol="000338"):
    return {
        "symbol": symbol,
        "price": 10.0,
        "change_pct": 0.01,
        "auction_amount_yuan": 100000,
        "bid_amount_yuan": 70000,
        "ask_amount_yuan": 20000,
    }


def _read(redis, symbol="000338"):
    return read_redis_auction_projection(
        redis,
        trade_date="2026-09-18",
        observed_at_ms=1789714800000,
        tags=("0920", "0924", "0925"),
        symbols=(symbol,),
    )


def test_real_redis_projection_traverses_public_engine_queue_without_guessing_price():
    redis = FakeRedis({tag: (_row(),) for tag in ("0920", "0924", "0925")})
    projections = _read(redis)

    result = MODULE.run_engine_shadow_from_redis(
        projections,
        trade_date="2026-09-18",
        symbol="000338",
    )

    assert redis.calls == [
        "market:auction:20260918:0920",
        "market:auction:20260918:0924",
        "market:auction:20260918:0925",
    ]
    assert result["read_only"] is True
    assert result["processed_signals"] == 6
    assert result["strategy_result_count"] == 3
    assert result["fact_only"] is True
    assert result["fact_status"] == "PARTIAL"
    assert all(value == "PARTIAL" for value in result["projection_statuses"].values())
    assert result["source_time_range"]["0920"]["oldest"] == 1789348803146
    assert result["source_time_range"]["0925"]["newest"] == 1789349106810
    assert "price_delta_milli" in result["fact_trace"]["metrics"]
    assert result["fact_trace"]["metrics"]["price_delta_milli"] is None
    # The raw Redis price is evidence only; the canonical Engine price remains missing.
    assert canonical_json(result)


def test_real_redis_missing_symbol_is_not_synthesized_into_engine_state():
    redis = FakeRedis({tag: (_row(),) for tag in ("0920", "0924", "0925")})
    projections = _read(redis, symbol="600519")

    result = MODULE.run_engine_shadow_from_redis(
        projections,
        trade_date="2026-09-18",
        symbol="600519",
    )

    assert result["projection_statuses"] == {
        "0920": "MISSING",
        "0924": "MISSING",
        "0925": "MISSING",
    }
    assert result["fact_status"] == "MISSING"
    assert result["fact_only"] is True
