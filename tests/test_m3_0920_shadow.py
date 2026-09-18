import json
import importlib.util
from datetime import datetime
from pathlib import Path

from engine_core import build_calendar_snapshot, local_datetime_ms, read_redis_auction_projection


SPEC = importlib.util.spec_from_file_location(
    "m3_0920_shadow",
    Path(__file__).parents[1] / "examples" / "run_m3_0920_shadow.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

TRADE_DATE = "2026-09-08"


class FakeRedis:
    def __init__(self, *, include_q2=True):
        self.include_q2 = include_q2
        self.calls = []

    def smembers(self, key):
        self.calls.append(("smembers", key))
        if key == "q2:active:20260908" and self.include_q2:
            return {"000001"}
        return set()

    def hgetall(self, key):
        self.calls.append(("hgetall", key))
        if key == "q2:000001" and self.include_q2:
            return {
                "px": "1000",
                "pc": "990",
                "amt": "100000",
                "ts": str(local_datetime_ms(TRADE_DATE, "09:19:59")),
                "mk": "sz",
            }
        if key == "market:auction:20260908:0920":
            ts = local_datetime_ms(TRADE_DATE, "09:19:59")
            return {
                "meta": json.dumps({"tag": "0920", "ts": ts, "n": 1}),
                "summary": json.dumps({"tag": "0920", "ts": ts}),
                "top_amount": json.dumps(
                    [
                        {
                            "symbol": "000001",
                            "price": 10.0,
                            "auction_amount_yuan": 100000,
                            "bid_amount_yuan": 70000,
                        }
                    ]
                ),
            }
        return {}


def _calendar():
    return build_calendar_snapshot(
        [TRADE_DATE],
        version="m3-0920-test-v1",
        declared_valid_from=TRADE_DATE,
        declared_valid_to=TRADE_DATE,
        source_guard_valid_from=TRADE_DATE,
        source_guard_valid_to=TRADE_DATE,
    )


def _projection(redis, observed_at):
    return read_redis_auction_projection(
        redis,
        trade_date=TRADE_DATE,
        observed_at_ms=observed_at,
        tags=("0920",),
        symbols=("000001",),
    )[0]


def test_m3_0920_prefetches_once_then_uses_one_engine_instance():
    redis = FakeRedis()
    observed = local_datetime_ms(TRADE_DATE, "09:19:59")
    as_of = local_datetime_ms(TRADE_DATE, "09:20:00")
    result = MODULE.run_m3_0920_shadow(
        client=redis,
        calendar=_calendar(),
        auction_projection=_projection(redis, observed),
        trade_date=TRADE_DATE,
        symbol="000001",
        observed_at=datetime.fromtimestamp(observed / 1000, MODULE.LOCAL_TZ),
        as_of=datetime.fromtimestamp(as_of / 1000, MODULE.LOCAL_TZ),
        stale_after_ms=60_000,
    )

    assert result["prefetch_calls"] == 1
    assert result["preflight_gate"] == "PASS"
    assert result["node_dispatched"] is True
    assert result["engine"]["same_engine_instance"] is True
    assert result["engine"]["processed_signals"] == 2
    assert result["engine"]["strategy_result_count"] == 1
    assert result["q2"]["status"] == "READY"
    assert result["timer"]["fired"]["origin"] == "NORMAL"


def test_m3_0920_preflight_failure_does_not_dispatch_or_fallback():
    redis = FakeRedis(include_q2=False)
    observed = local_datetime_ms(TRADE_DATE, "09:19:59")
    as_of = local_datetime_ms(TRADE_DATE, "09:20:00")
    result = MODULE.run_m3_0920_shadow(
        client=redis,
        calendar=_calendar(),
        auction_projection=_projection(redis, observed),
        trade_date=TRADE_DATE,
        symbol="000001",
        observed_at=datetime.fromtimestamp(observed / 1000, MODULE.LOCAL_TZ),
        as_of=datetime.fromtimestamp(as_of / 1000, MODULE.LOCAL_TZ),
        stale_after_ms=60_000,
    )

    assert result["q2"]["status"] == "MISSING"
    assert result["preflight_gate"] == "BLOCKED"
    assert result["node_dispatched"] is False
    assert result["engine"] is None
    assert result["preflight_failure_is_fail_closed"] is True


def test_m3_0920_recovery_does_not_retrofit_current_q2_to_old_anchor():
    redis = FakeRedis()
    observed = local_datetime_ms(TRADE_DATE, "09:30:00")
    as_of = observed
    result = MODULE.run_m3_0920_shadow(
        client=redis,
        calendar=_calendar(),
        auction_projection=_projection(redis, observed),
        trade_date=TRADE_DATE,
        symbol="000001",
        observed_at=datetime.fromtimestamp(observed / 1000, MODULE.LOCAL_TZ),
        as_of=datetime.fromtimestamp(as_of / 1000, MODULE.LOCAL_TZ),
        stale_after_ms=60_000,
        origin="RECOVERY_CATCHUP",
    )

    assert result["preflight_gate"] == "BLOCKED"
    assert result["node_dispatched"] is False
    assert "q2_observed_after_0920_firing" in result["startup_self_check"]["reasons"]


def test_m3_0920_missing_auction_projection_does_not_dispatch_engine():
    redis = FakeRedis()
    observed = local_datetime_ms(TRADE_DATE, "09:19:59")
    as_of = local_datetime_ms(TRADE_DATE, "09:20:00")
    missing_projection = read_redis_auction_projection(
        redis,
        trade_date=TRADE_DATE,
        observed_at_ms=observed,
        tags=("0920",),
        symbols=("600519",),
    )[0]
    result = MODULE.run_m3_0920_shadow(
        client=redis,
        calendar=_calendar(),
        auction_projection=missing_projection,
        trade_date=TRADE_DATE,
        symbol="600519",
        observed_at=datetime.fromtimestamp(observed / 1000, MODULE.LOCAL_TZ),
        as_of=datetime.fromtimestamp(as_of / 1000, MODULE.LOCAL_TZ),
        stale_after_ms=60_000,
    )

    assert result["preflight_gate"] == "BLOCKED"
    assert result["node_dispatched"] is False
    assert result["engine"] is None
    assert "auction_projection_status:MISSING" in result["startup_self_check"]["reasons"]
