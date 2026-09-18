import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from engine_core import (
    FreshnessPolicy,
    RedisQ2ProjectionAdapter,
    build_q2_projection,
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
    def __init__(self, *, source_time_ms: int = 1000):
        self.source_time_ms = source_time_ms

    def smembers(self, key):
        return {b"600519"}

    def hgetall(self, key):
        return {
            b"mk": b"SH",
            b"px": b"10500",
            b"pc": b"10000",
            b"amt": b"1200000",
            b"vol": b"100",
            b"ts": str(self.source_time_ms).encode(),
            b"amt2m": b"50000",
            b"ls": b"1",
        }


class _AuctionAndQ2Redis(_FakeRedis):
    def __init__(self, *, source_time_ms: int, auction_source_times=None):
        super().__init__(source_time_ms=source_time_ms)
        self.auction_source_times = dict(auction_source_times or {})

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
                "meta": json.dumps(
                    {
                        "tag": tag,
                        "ts": self.auction_source_times.get(tag, self.source_time_ms),
                    }
                ),
                "summary": json.dumps({"tag": tag}),
                "top_amount": json.dumps([row]),
            }
        return super().hgetall(key)


def _q2_projection(trade_date: str, source_time: str, observed_time: str):
    observed = datetime.fromisoformat(
        f"{trade_date}T{observed_time}+08:00"
    ).astimezone(timezone.utc)
    source = datetime.fromisoformat(
        f"{trade_date}T{source_time}+08:00"
    ).astimezone(timezone.utc)
    return build_q2_projection(
        trade_date,
        observed,
        ("600519",),
        {
            "600519": {
                "mk": "sh",
                "px": "105000",
                "pc": "100000",
                "amt": "1200000",
                "vol": "100",
                "ts": str(int(source.timestamp() * 1000)),
            }
        },
        freshness_policy=FreshnessPolicy(stale_after_ms=60_000),
    )


def _evaluation_times(trade_date: str, *, auction_0925: str = "09:25:06"):
    return {
        tag: int(
            datetime.fromisoformat(f"{trade_date}T{clock}+08:00").timestamp() * 1000
        )
        for tag, clock in (
            ("0920", "09:20:03"),
            ("0924", "09:24:10"),
            ("0925", auction_0925),
            ("OPENING_0932", "09:32:00"),
        )
    }


def _fixture_rows_without_final_anchor(fixture: dict):
    rows = []
    for item in fixture["snapshots"].values():
        tag = item["trigger_id"].split("_")[-1]
        if tag not in {"0920", "0924"}:
            continue
        rows.append(
            {
                "ts": datetime.fromtimestamp(
                    item["source_record_time_ms"] / 1000,
                    tz=timezone.utc,
                ),
                "px_milli": item["state"].get("price_milli"),
                "match_amt_yuan": item["state"].get("auction_amount_yuan"),
                "rest_bid_amt_yuan": item["state"].get("auction_bid_amount_yuan"),
                "rest_ask_amt_yuan": item["state"].get("auction_ask_amount_yuan"),
                "symbol": fixture["symbol"],
                "trade_date": fixture["trade_date"].replace("-", ""),
                "auction_tag": tag,
            }
        )
    row_0925 = dict(rows[-1])
    row_0925["auction_tag"] = "0925"
    # The production finalization firing is 09:25:06: the source does not
    # contain the complete auction cohort at the wall anchor 09:25:00.
    row_0925["ts"] = row_0925["ts"] + timedelta(seconds=56)
    rows.append(row_0925)
    return rows


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
    row_0925["ts"] = row_0924["ts"] + timedelta(seconds=56)
    rows.append(row_0925)
    projection = _q2_projection(fixture["trade_date"], "09:32:00", "09:32:00")
    result = MODULE.run_continuous_session_shadow(
        auction_rows=rows,
        opening_projection=projection,
        trade_date=fixture["trade_date"],
        symbol="600519",
        evaluation_times_ms=_evaluation_times(fixture["trade_date"]),
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


def test_continuous_shadow_rejects_cross_trade_date_projection():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows = []
    for item in fixture["snapshots"].values():
        tag = item["trigger_id"].split("_")[-1]
        if tag not in {"0920", "0924"}:
            continue
        rows.append(
            {
                "ts": datetime.fromtimestamp(
                    item["source_record_time_ms"] / 1000,
                    tz=timezone.utc,
                ),
                "px_milli": item["state"].get("price_milli"),
                "match_amt_yuan": item["state"].get("auction_amount_yuan"),
                "rest_bid_amt_yuan": item["state"].get("auction_bid_amount_yuan"),
                "rest_ask_amt_yuan": item["state"].get("auction_ask_amount_yuan"),
                "symbol": fixture["symbol"],
                "trade_date": fixture["trade_date"].replace("-", ""),
                "auction_tag": tag,
            }
        )
    row_0925 = dict(rows[-1])
    row_0925["auction_tag"] = "0925"
    row_0925["ts"] = row_0925["ts"] + timedelta(seconds=60)
    rows.append(row_0925)
    with pytest.raises(ValueError, match="trade_date"):
        MODULE.run_continuous_session_shadow(
            auction_rows=rows,
            opening_projection=_q2_projection("2026-09-02", "09:32:00", "09:32:00"),
            trade_date=fixture["trade_date"],
            symbol="600519",
            evaluation_times_ms=_evaluation_times(fixture["trade_date"]),
        )


def test_continuous_shadow_rejects_future_opening_source_time():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="source time"):
        MODULE.run_continuous_session_shadow(
            auction_rows=_fixture_rows_without_final_anchor(fixture),
            opening_projection=_q2_projection(
                fixture["trade_date"], "09:32:01", "09:32:00"
            ),
            trade_date=fixture["trade_date"],
            symbol="600519",
            evaluation_times_ms=_evaluation_times(fixture["trade_date"]),
        )


def test_continuous_shadow_rejects_auction_source_after_explicit_evaluation_time():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="after node cutoff"):
        MODULE.run_continuous_session_shadow(
            auction_rows=_fixture_rows_without_final_anchor(fixture),
            opening_projection=_q2_projection(
                fixture["trade_date"], "09:32:00", "09:32:00"
            ),
            trade_date=fixture["trade_date"],
            symbol="600519",
            evaluation_times_ms=_evaluation_times(
                fixture["trade_date"], auction_0925="09:25:05"
            ),
        )


def test_continuous_shadow_rejects_evaluation_times_on_another_trade_date():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    evaluation_times = {
        key: value + 86_400_000
        for key, value in _evaluation_times(fixture["trade_date"]).items()
    }
    with pytest.raises(ValueError, match="crosses trade date"):
        MODULE.run_continuous_session_shadow(
            auction_rows=_fixture_rows_without_final_anchor(fixture),
            opening_projection=_q2_projection(
                fixture["trade_date"], "09:32:00", "09:32:00"
            ),
            trade_date=fixture["trade_date"],
            symbol="600519",
            evaluation_times_ms=evaluation_times,
        )


def test_continuous_shadow_rejects_evaluation_before_0925_business_anchor():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    evaluation_times = _evaluation_times(fixture["trade_date"])
    evaluation_times["0925"] -= 7_000
    with pytest.raises(ValueError, match="before business anchor"):
        MODULE.run_continuous_session_shadow(
            auction_rows=_fixture_rows_without_final_anchor(fixture),
            opening_projection=_q2_projection(
                fixture["trade_date"], "09:32:00", "09:32:00"
            ),
            trade_date=fixture["trade_date"],
            symbol="600519",
            evaluation_times_ms=evaluation_times,
        )


def test_continuous_redis_shadow_preserves_missing_0924_without_substitution():
    from engine_core import FreshnessPolicy, RedisQ2ProjectionAdapter

    trade_date = "2026-09-03"
    redis = _AuctionAndQ2Redis(
        source_time_ms=int(
            datetime.fromisoformat(f"{trade_date}T09:32:00+08:00").timestamp() * 1000
        ),
        auction_source_times={
            tag: int(datetime.fromisoformat(f"{trade_date}T{clock}+08:00").timestamp() * 1000)
            for tag, clock in (("0920", "09:20:03"), ("0925", "09:25:06"))
        },
    )
    auction = []
    for tag, observed_time in (
        ("0920", "09:20:03"),
        ("0924", "09:24:10"),
        ("0925", "09:25:06"),
    ):
        observed_ms = int(
            datetime.fromisoformat(f"{trade_date}T{observed_time}+08:00").timestamp()
            * 1000
        )
        auction.extend(
            read_redis_auction_projection(
                redis,
                trade_date=trade_date,
                observed_at_ms=observed_ms,
                tags=(tag,),
                symbols=("600519",),
            )
        )
    opening = RedisQ2ProjectionAdapter(redis).read(
        trade_date,
        datetime.fromisoformat(f"{trade_date}T09:32:00+08:00"),
        freshness_policy=FreshnessPolicy(stale_after_ms=60_000),
    )
    result = MODULE.run_continuous_redis_session_shadow(
        auction_projections=auction,
        opening_projection=opening,
        trade_date=trade_date,
        symbol="600519",
        evaluation_times_ms=_evaluation_times(trade_date, auction_0925="09:25:06"),
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

    trade_date = "2026-09-03"
    redis = _AuctionAndQ2Redis(
        source_time_ms=int(
            datetime.fromisoformat(f"{trade_date}T09:32:00+08:00").timestamp() * 1000
        ),
        auction_source_times={
            tag: int(datetime.fromisoformat(f"{trade_date}T{clock}+08:00").timestamp() * 1000)
            for tag, clock in (("0920", "09:20:03"), ("0925", "09:25:06"))
        },
    )
    auction = []
    for tag, observed_time in (
        ("0920", "09:20:03"),
        ("0924", "09:24:10"),
        ("0925", "09:25:06"),
    ):
        observed_ms = int(
            datetime.fromisoformat(f"{trade_date}T{observed_time}+08:00").timestamp()
            * 1000
        )
        auction.extend(
            read_redis_auction_projection(
                redis,
                trade_date=trade_date,
                observed_at_ms=observed_ms,
                tags=(tag,),
                symbols=("600519",),
            )
        )
    opening = RedisQ2ProjectionAdapter(redis).read(
        trade_date,
        datetime.fromisoformat(f"{trade_date}T09:32:00+08:00"),
        freshness_policy=FreshnessPolicy(stale_after_ms=60_000),
    )
    duplicate = tuple(auction) + (auction[0],)
    try:
        MODULE.run_continuous_redis_session_shadow(
            auction_projections=duplicate,
            opening_projection=opening,
            trade_date=trade_date,
            symbol="600519",
            evaluation_times_ms=_evaluation_times(trade_date, auction_0925="09:25:06"),
        )
    except ValueError as exc:
        assert "duplicate Redis auction tag" in str(exc)
    else:
        raise AssertionError("duplicate Redis auction tag was accepted")
