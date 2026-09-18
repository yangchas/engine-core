import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine_core import (
    FreshnessPolicy,
    RedisQ2ProjectionAdapter,
    read_redis_auction_projection,
)


SPEC = importlib.util.spec_from_file_location(
    "continuous_session_shadow",
    Path(__file__).parents[1] / "examples" / "run_continuous_session_shadow.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


FIXTURE = Path(__file__).parent / "fixtures" / "facts" / "auction_600519_20260903.json"


class _FakeRedis:
    def smembers(self, key):
        return {b"600519"}

    def hgetall(self, key):
        return {
            b"mk": b"SH",
            b"px": b"10500",
            b"pc": b"10000",
            b"amt": b"1200000",
            b"vol": b"100",
            b"ts": b"1000",
            b"amt2m": b"50000",
            b"ls": b"1",
        }


class _AuctionAndQ2Redis(_FakeRedis):
    def hgetall(self, key):
        if key.startswith("market:auction:"):
            if key.endswith(":0924"):
                return {}
            tag = key.rsplit(":", 1)[-1]
            row = {
                "symbol": "600519",
                "auction_amount_yuan": 1200000 if tag == "0920" else 1300000,
                "bid_amount_yuan": 700000,
                "ask_amount_yuan": 200000,
                "price": 105.0,
            }
            return {
                "meta": json.dumps({"tag": tag, "ts": 1000}),
                "summary": json.dumps({"tag": tag}),
                "top_amount": json.dumps([row]),
            }
        return super().hgetall(key)


def test_continuous_shadow_reuses_one_engine_for_auction_and_opening():
    import json

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows = []
    for item in fixture["snapshots"].values():
        tag = item["trigger_id"].split("_")[-1]
        if tag not in {"0920", "0924", "0925"}:
            continue
        source_time = datetime.fromtimestamp(
            item["source_record_time_ms"] / 1000,
            tz=timezone.utc,
        )
        rows.append(
            {
                "ts": source_time,
                "px_milli": item["state"].get("price_milli"),
                "match_amt_yuan": item["state"].get("auction_amount_yuan"),
                "rest_bid_amt_yuan": item["state"].get("auction_bid_amount_yuan"),
                "rest_ask_amt_yuan": item["state"].get("auction_ask_amount_yuan"),
                "symbol": fixture["symbol"],
                "trade_date": fixture["trade_date"].replace("-", ""),
                "auction_tag": tag,
            }
        )
    # This contract test uses a clearly synthetic final anchor only because
    # the frozen wheel fixture predates the 0925 row.  Production callers must
    # pass an observed 0925 row; the adapter rejects a missing anchor.
    row_0924 = next(item for item in rows if item["auction_tag"] == "0924")
    row_0925 = dict(row_0924)
    row_0925["auction_tag"] = "0925"
    row_0925["ts"] = row_0924["ts"] + timedelta(seconds=60)
    rows.append(row_0925)
    projection = RedisQ2ProjectionAdapter(_FakeRedis()).read(
        "1970-01-01",
        datetime.fromtimestamp(1000, tz=timezone.utc),
        freshness_policy=FreshnessPolicy(stale_after_ms=100000000000),
    )
    result = MODULE.run_continuous_session_shadow(
        auction_rows=rows,
        opening_projection=projection,
        trade_date=fixture["trade_date"],
        symbol="600519",
    )
    assert result["single_engine"] is True
    assert result["processed_signals"] == 8
    assert result["strategy_result_count"] == 4
    assert result["pending_evaluations"] == ()
    json.dumps(result, ensure_ascii=False, sort_keys=True)
    assert [item["trigger_id"] for item in result["strategy_results"]] == [
        "AUCTION_0920",
        "AUCTION_0924",
        "AUCTION_0925",
        "OPENING_0932",
    ]
    assert result["strategy_results"][-1]["delegated_strategy_id"] == "opening-shadow-v1"


def test_continuous_redis_shadow_preserves_missing_0924_without_substitution():
    from engine_core import FreshnessPolicy, RedisQ2ProjectionAdapter

    redis = _AuctionAndQ2Redis()
    auction = read_redis_auction_projection(
        redis,
        trade_date="1970-01-01",
        observed_at_ms=1000,
        tags=("0920", "0924", "0925"),
        symbols=("600519",),
    )
    opening = RedisQ2ProjectionAdapter(redis).read(
        "1970-01-01",
        datetime.fromtimestamp(1000, tz=timezone.utc),
        freshness_policy=FreshnessPolicy(stale_after_ms=100000000000),
    )
    result = MODULE.run_continuous_redis_session_shadow(
        auction_projections=auction,
        opening_projection=opening,
        trade_date="1970-01-01",
        symbol="600519",
    )
    assert result["single_engine"] is True
    assert result["processed_signals"] == 8
    assert result["strategy_result_count"] == 4
    assert result["strategy_results"][1]["trigger_id"] == "AUCTION_0924"
    # 0924 itself cannot compare until the 0925 close anchor arrives; the
    # later 0925 result must expose the missing middle anchor instead of
    # substituting 0920 or inventing a segment.
    assert result["strategy_results"][1]["child_trace"]["fact_status"] == "PENDING"
    assert result["strategy_results"][2]["child_trace"]["fact_status"] == "MISSING"
    json.dumps(result, ensure_ascii=False, sort_keys=True)


def test_continuous_redis_shadow_rejects_duplicate_anchor_tags():
    from engine_core import FreshnessPolicy, RedisQ2ProjectionAdapter

    redis = _AuctionAndQ2Redis()
    auction = read_redis_auction_projection(
        redis,
        trade_date="1970-01-01",
        observed_at_ms=1000,
        tags=("0920", "0924", "0925"),
        symbols=("600519",),
    )
    opening = RedisQ2ProjectionAdapter(redis).read(
        "1970-01-01",
        datetime.fromtimestamp(1000, tz=timezone.utc),
        freshness_policy=FreshnessPolicy(stale_after_ms=100000000000),
    )
    duplicate = tuple(auction) + (auction[0],)
    try:
        MODULE.run_continuous_redis_session_shadow(
            auction_projections=duplicate,
            opening_projection=opening,
            trade_date="1970-01-01",
            symbol="600519",
        )
    except ValueError as exc:
        assert "duplicate Redis auction tag" in str(exc)
    else:
        raise AssertionError("duplicate Redis auction tag was accepted")
