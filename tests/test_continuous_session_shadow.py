import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine_core import FreshnessPolicy, RedisQ2ProjectionAdapter


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
    assert [item["trigger_id"] for item in result["strategy_results"]] == [
        "AUCTION_0920",
        "AUCTION_0924",
        "AUCTION_0925",
        "OPENING_0932",
    ]
    assert result["strategy_results"][-1]["delegated_strategy_id"] == "opening-shadow-v1"
