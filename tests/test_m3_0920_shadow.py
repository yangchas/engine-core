import json
import importlib.util
import sys
import types
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

    def close(self):
        return None


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


def test_m3_0920_accepts_already_read_q2_without_second_redis_read():
    redis = FakeRedis()
    observed = local_datetime_ms(TRADE_DATE, "09:19:59")
    observed_dt = datetime.fromtimestamp(observed / 1000, MODULE.LOCAL_TZ)
    as_of = local_datetime_ms(TRADE_DATE, "09:20:00")
    projection = _projection(redis, observed)
    q2 = MODULE.RedisQ2ProjectionAdapter(redis).read(
        TRADE_DATE,
        observed_dt,
        freshness_policy=MODULE.FreshnessPolicy(stale_after_ms=60_000),
    )
    redis.calls.clear()

    result = MODULE.run_m3_0920_shadow(
        client=redis,
        calendar=_calendar(),
        auction_projection=projection,
        trade_date=TRADE_DATE,
        symbol="000001",
        observed_at=observed_dt,
        as_of=datetime.fromtimestamp(as_of / 1000, MODULE.LOCAL_TZ),
        stale_after_ms=60_000,
        q2_snapshot=q2,
        preflight_at=observed_dt,
    )

    assert result["preflight_gate"] == "PASS"
    assert result["prefetch_calls"] == 1
    assert redis.calls == []


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


def test_m3_0920_normal_after_capture_window_is_blocked_without_source_read():
    redis = FakeRedis()
    observed = local_datetime_ms(TRADE_DATE, "09:30:00")
    projection = _projection(redis, observed)
    redis.calls.clear()
    result = MODULE.run_m3_0920_shadow(
        client=redis,
        calendar=_calendar(),
        auction_projection=projection,
        trade_date=TRADE_DATE,
        symbol="000001",
        observed_at=datetime.fromtimestamp(observed / 1000, MODULE.LOCAL_TZ),
        as_of=datetime.fromtimestamp(observed / 1000, MODULE.LOCAL_TZ),
        stale_after_ms=60_000,
        origin="NORMAL",
    )

    assert result["preflight_gate"] == "BLOCKED"
    assert result["preflight_failure_is_fail_closed"] is True
    assert result["startup_self_check"]["reasons"] == ("normal_capture_window_expired",)
    assert result["prefetch_calls"] == 0
    assert result["node_dispatched"] is False
    assert result["engine"] is None
    assert redis.calls == []


def test_m3_0920_normal_before_capture_window_is_blocked_without_source_read():
    redis = FakeRedis()
    observed = local_datetime_ms(TRADE_DATE, "09:10:00")
    result = MODULE.run_m3_0920_shadow(
        client=redis,
        calendar=_calendar(),
        auction_projection=None,
        trade_date=TRADE_DATE,
        symbol="000001",
        observed_at=datetime.fromtimestamp(observed / 1000, MODULE.LOCAL_TZ),
        as_of=datetime.fromtimestamp(observed / 1000, MODULE.LOCAL_TZ),
        stale_after_ms=60_000,
        origin="NORMAL",
    )

    assert result["preflight_gate"] == "BLOCKED"
    assert result["preflight_failure_is_fail_closed"] is True
    assert result["startup_self_check"]["reasons"] == (
        "normal_capture_window_not_started",
    )
    assert result["prefetch_calls"] == 0
    assert result["node_dispatched"] is False
    assert result["engine"] is None
    assert redis.calls == []


def test_m3_0920_normal_capture_window_boundaries_are_explicit():
    def at(clock_time: str) -> datetime:
        epoch = local_datetime_ms(TRADE_DATE, clock_time)
        return datetime.fromtimestamp(epoch / 1000, MODULE.LOCAL_TZ)

    assert MODULE._normal_capture_window_reason(at("09:14:59")) == (
        "normal_capture_window_not_started"
    )
    assert MODULE._normal_capture_window_reason(at("09:15:00")) is None
    assert MODULE._normal_capture_window_reason(at("09:20:59")) is None
    assert MODULE._normal_capture_window_reason(at("09:21:00")) is None
    assert MODULE._normal_capture_window_reason(at("09:21:01")) == (
        "normal_capture_window_expired"
    )


def test_m3_0920_cli_does_not_read_redis_outside_normal_window(
    tmp_path: Path, monkeypatch
):
    redis = FakeRedis()
    calendar = _calendar()
    calendar_file = tmp_path / "calendar.json"
    calendar_file.write_text(
        json.dumps(
            {
                "calendar_id": calendar.calendar_id,
                "version": calendar.version,
                "timezone": calendar.timezone_name,
                "declared_valid_from": calendar.declared_valid_from,
                "declared_valid_to": calendar.declared_valid_to,
                "source_guard_valid_from": calendar.source_guard_valid_from,
                "source_guard_valid_to": calendar.source_guard_valid_to,
                "trading_dates": list(calendar.trading_dates),
                "calendar_semantic_hash": calendar.semantic_hash,
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "m3.json"
    fake_redis_module = types.SimpleNamespace(Redis=lambda **_: redis)
    monkeypatch.setitem(sys.modules, "redis", fake_redis_module)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_m3_0920_shadow.py",
            "--trade-date",
            TRADE_DATE,
            "--symbol",
            "000001",
            "--calendar-file",
            str(calendar_file),
            "--observed-at",
            "09:10:00",
            "--as-of",
            "09:10:00",
            "--stale-after-ms",
            "60000",
            "--output",
            str(output),
        ],
    )

    assert MODULE.main() == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["preflight_gate"] == "BLOCKED"
    assert result["prefetch_calls"] == 0
    assert result["node_dispatched"] is False
    assert redis.calls == []


def test_m3_0920_cli_timer_not_due_does_not_consume_output_path(
    tmp_path: Path, monkeypatch
):
    redis = FakeRedis()
    calendar = _calendar()
    calendar_file = tmp_path / "calendar.json"
    calendar_file.write_text(
        json.dumps(
            {
                "calendar_id": calendar.calendar_id,
                "version": calendar.version,
                "timezone": calendar.timezone_name,
                "declared_valid_from": calendar.declared_valid_from,
                "declared_valid_to": calendar.declared_valid_to,
                "source_guard_valid_from": calendar.source_guard_valid_from,
                "source_guard_valid_to": calendar.source_guard_valid_to,
                "trading_dates": list(calendar.trading_dates),
                "calendar_semantic_hash": calendar.semantic_hash,
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "m3.json"
    fake_redis_module = types.SimpleNamespace(Redis=lambda **_: redis)
    monkeypatch.setitem(sys.modules, "redis", fake_redis_module)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_m3_0920_shadow.py",
            "--trade-date",
            TRADE_DATE,
            "--symbol",
            "000001",
            "--calendar-file",
            str(calendar_file),
            "--observed-at",
            "09:19:00",
            "--as-of",
            "09:19:00",
            "--stale-after-ms",
            "60000",
            "--output",
            str(output),
        ],
    )

    assert MODULE.main() == 2
    assert not output.exists()
    assert redis.calls


def test_m3_0920_rejects_observation_date_mismatch_before_source_read():
    redis = FakeRedis()
    observed = datetime.fromisoformat("2026-09-20T09:19:59+08:00")
    as_of = datetime.fromisoformat("2026-09-20T09:20:00+08:00")
    try:
        MODULE.run_m3_0920_shadow(
            client=redis,
            calendar=_calendar(),
            auction_projection=None,
            trade_date=TRADE_DATE,
            symbol="000001",
            observed_at=observed,
            as_of=as_of,
            stale_after_ms=60_000,
        )
    except ValueError as exc:
        assert str(exc) == "observed_at local date does not match trade_date"
    else:
        raise AssertionError("expected observation date mismatch")
    assert redis.calls == []


def test_m3_0920_rejects_as_of_date_mismatch_before_source_read():
    redis = FakeRedis()
    observed = datetime.fromisoformat("2026-09-08T09:19:59+08:00")
    as_of = datetime.fromisoformat("2026-09-09T09:20:00+08:00")
    try:
        MODULE.run_m3_0920_shadow(
            client=redis,
            calendar=_calendar(),
            auction_projection=None,
            trade_date=TRADE_DATE,
            symbol="000001",
            observed_at=observed,
            as_of=as_of,
            stale_after_ms=60_000,
        )
    except ValueError as exc:
        assert str(exc) == "as_of local date does not match trade_date"
    else:
        raise AssertionError("expected as_of date mismatch")
    assert redis.calls == []


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
