from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

from engine_core import build_calendar_snapshot, local_datetime_ms, read_redis_auction_projection


SPEC = importlib.util.spec_from_file_location(
    "m3_auction_followup_shadow",
    Path(__file__).parents[1] / "examples" / "run_m3_auction_followup_shadow.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

TRADE_DATE = "2026-09-08"


def _source_ms(clock: str) -> int:
    if "." in clock:
        return int(
            datetime.fromisoformat(f"{TRADE_DATE}T{clock}+08:00").timestamp()
            * 1000
        )
    return local_datetime_ms(TRADE_DATE, clock)


class FakeRedis:
    def __init__(
        self,
        *,
        future_tag: str | None = None,
        source_clock_0925: str = "09:25:06",
    ) -> None:
        self.calls = []
        self.future_tag = future_tag
        self.source_clock_0925 = source_clock_0925

    def hgetall(self, key):
        self.calls.append(("hgetall", key))
        tag = key.rsplit(":", 1)[-1]
        if tag not in {"0920", "0924", "0925"}:
            return {}
        source_clock = (
            self.source_clock_0925
            if tag == "0925"
            else f"{tag[:2]}:{tag[2:]}:00"
        )
        ts = _source_ms(source_clock)
        if tag == self.future_tag:
            ts = local_datetime_ms(TRADE_DATE, "09:30:00")
        return {
            "meta": json.dumps({"tag": tag, "ts": ts, "n": 1}),
            "summary": json.dumps({"tag": tag, "ts": ts}),
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


def _calendar():
    return build_calendar_snapshot(
        [TRADE_DATE],
        version="m3-followup-test-v1",
        declared_valid_from=TRADE_DATE,
        declared_valid_to=TRADE_DATE,
        source_guard_valid_from=TRADE_DATE,
        source_guard_valid_to=TRADE_DATE,
    )


def _projections(redis, observed_ms, tags=("0920", "0924", "0925")):
    return {
        item.tag: item
        for item in read_redis_auction_projection(
            redis,
            trade_date=TRADE_DATE,
            observed_at_ms=observed_ms,
            tags=tags,
            symbols=("000001",),
        )
    }


def _dt(ms):
    return datetime.fromtimestamp(ms / 1000, MODULE.LOCAL_TZ)


def test_0924_normal_node_uses_existing_projection_and_one_engine():
    redis = FakeRedis()
    observed = local_datetime_ms(TRADE_DATE, "09:24:05")
    projections = _projections(redis, observed)
    result = MODULE.run_m3_auction_followup_shadow(
        calendar=_calendar(),
        current_projection=projections["0924"],
        prior_projections={"0920": projections["0920"]},
        trade_date=TRADE_DATE,
        symbol="000001",
        node_tag="0924",
        observed_at=_dt(observed),
        as_of=_dt(local_datetime_ms(TRADE_DATE, "09:24:10")),
    )
    assert result["preflight_gate"] == "PASS"
    assert result["node_dispatched"] is True
    assert result["startup_self_check"]["node_readiness"] == "DISPATCHABLE"
    assert result["startup_self_check"]["q2_policy"] == "OPTIONAL_FOR_SOURCE_OWNED_AUCTION_NODE"
    assert result["engine"]["same_engine_instance"] is True
    assert result["engine"]["processed_signals"] == 2


def test_0925_requires_both_prior_anchors_and_does_not_backfill():
    redis = FakeRedis()
    observed = local_datetime_ms(TRADE_DATE, "09:25:05")
    projections = _projections(redis, observed)
    result = MODULE.run_m3_auction_followup_shadow(
        calendar=_calendar(),
        current_projection=projections["0925"],
        prior_projections={"0920": projections["0920"]},
        trade_date=TRADE_DATE,
        symbol="000001",
        node_tag="0925",
        observed_at=_dt(observed),
        as_of=_dt(observed),
    )
    assert result["preflight_gate"] == "BLOCKED"
    assert result["node_dispatched"] is False
    assert "missing_prior_projection:0924" in result["startup_self_check"]["reasons"]


def test_0925_normal_waits_for_six_second_settling_barrier():
    redis = FakeRedis()
    observed = local_datetime_ms(TRADE_DATE, "09:25:05")
    projections = _projections(redis, observed)
    result = MODULE.run_m3_auction_followup_shadow(
        calendar=_calendar(),
        current_projection=projections["0925"],
        prior_projections={"0920": projections["0920"], "0924": projections["0924"]},
        trade_date=TRADE_DATE,
        symbol="000001",
        node_tag="0925",
        observed_at=_dt(observed),
        as_of=_dt(observed),
    )
    assert result["preflight_gate"] == "BLOCKED"
    assert result["startup_self_check"]["reasons"] == (
        "auction_0925_finalization_barrier_not_reached",
    )


def test_0925_normal_is_admissible_at_six_second_settling_barrier():
    redis = FakeRedis()
    observed = local_datetime_ms(TRADE_DATE, "09:25:06")
    projections = _projections(redis, observed)
    result = MODULE.run_m3_auction_followup_shadow(
        calendar=_calendar(),
        current_projection=projections["0925"],
        prior_projections={"0920": projections["0920"], "0924": projections["0924"]},
        trade_date=TRADE_DATE,
        symbol="000001",
        node_tag="0925",
        observed_at=_dt(observed),
        as_of=_dt(observed),
    )
    assert result["preflight_gate"] == "PASS"
    assert result["node_dispatched"] is True


def test_0925_barrier_is_earliest_admission_not_source_timestamp_rewrite():
    redis = FakeRedis(source_clock_0925="09:25:06.197")
    barrier = local_datetime_ms(TRADE_DATE, "09:25:06")
    projections = _projections(redis, barrier)
    too_early = MODULE.run_m3_auction_followup_shadow(
        calendar=_calendar(),
        current_projection=projections["0925"],
        prior_projections={"0920": projections["0920"], "0924": projections["0924"]},
        trade_date=TRADE_DATE,
        symbol="000001",
        node_tag="0925",
        observed_at=_dt(barrier),
        as_of=_dt(barrier),
    )
    assert too_early["preflight_gate"] == "BLOCKED"
    assert too_early["startup_self_check"]["reasons"] == (
        "projection_source_time_after_0925_cutoff",
    )

    actual_source_time = _source_ms("09:25:06.197")
    projections = _projections(redis, actual_source_time)
    admitted = MODULE.run_m3_auction_followup_shadow(
        calendar=_calendar(),
        current_projection=projections["0925"],
        prior_projections={"0920": projections["0920"], "0924": projections["0924"]},
        trade_date=TRADE_DATE,
        symbol="000001",
        node_tag="0925",
        observed_at=_dt(actual_source_time),
        as_of=_dt(actual_source_time),
    )
    assert admitted["preflight_gate"] == "PASS"
    assert admitted["node_dispatched"] is True


def test_late_normal_followup_is_blocked_without_engine_dispatch():
    redis = FakeRedis()
    observed = local_datetime_ms(TRADE_DATE, "09:30:00")
    projections = _projections(redis, observed)
    redis.calls.clear()
    result = MODULE.run_m3_auction_followup_shadow(
        calendar=_calendar(),
        current_projection=projections["0924"],
        prior_projections={"0920": projections["0920"]},
        trade_date=TRADE_DATE,
        symbol="000001",
        node_tag="0924",
        observed_at=_dt(observed),
        as_of=_dt(observed),
    )
    assert result["preflight_gate"] == "BLOCKED"
    assert result["node_dispatched"] is False
    assert result["startup_self_check"]["reasons"] == ("normal_capture_window_invalid",)
    assert redis.calls == []


def test_recovery_never_retrofits_projection_observed_after_anchor():
    redis = FakeRedis()
    observed = local_datetime_ms(TRADE_DATE, "09:30:00")
    projections = _projections(redis, observed)
    result = MODULE.run_m3_auction_followup_shadow(
        calendar=_calendar(),
        current_projection=projections["0925"],
        prior_projections={"0920": projections["0920"], "0924": projections["0924"]},
        trade_date=TRADE_DATE,
        symbol="000001",
        node_tag="0925",
        observed_at=_dt(observed),
        as_of=_dt(observed),
        origin="RECOVERY_CATCHUP",
    )
    assert result["preflight_gate"] == "BLOCKED"
    assert result["node_dispatched"] is False
    assert result["startup_self_check"]["reasons"] == ("projection_observed_after_0925_cutoff",)


def test_0925_recovery_uses_six_second_source_cutoff():
    redis = FakeRedis()
    observed = local_datetime_ms(TRADE_DATE, "09:25:06")
    projections = _projections(redis, observed)
    result = MODULE.run_m3_auction_followup_shadow(
        calendar=_calendar(),
        current_projection=projections["0925"],
        prior_projections={"0920": projections["0920"], "0924": projections["0924"]},
        trade_date=TRADE_DATE,
        symbol="000001",
        node_tag="0925",
        observed_at=_dt(observed),
        as_of=_dt(local_datetime_ms(TRADE_DATE, "09:26:00")),
        origin="RECOVERY_CATCHUP",
    )
    assert result["preflight_gate"] == "PASS"
    assert result["node_dispatched"] is True


def test_prior_projection_key_cannot_mask_a_wrong_projection_tag():
    redis = FakeRedis()
    observed = local_datetime_ms(TRADE_DATE, "09:24:05")
    projections = _projections(redis, observed)
    result = MODULE.run_m3_auction_followup_shadow(
        calendar=_calendar(),
        current_projection=projections["0924"],
        prior_projections={"0920": projections["0924"]},
        trade_date=TRADE_DATE,
        symbol="000001",
        node_tag="0924",
        observed_at=_dt(observed),
        as_of=_dt(local_datetime_ms(TRADE_DATE, "09:24:10")),
    )
    assert result["preflight_gate"] == "BLOCKED"
    assert result["startup_self_check"]["reasons"] == ("prior_projection_tag_mismatch:0920",)


def test_future_source_record_time_is_not_admitted_to_followup_cutoff():
    redis = FakeRedis(future_tag="0924")
    observed = local_datetime_ms(TRADE_DATE, "09:24:05")
    projections = _projections(redis, observed)
    result = MODULE.run_m3_auction_followup_shadow(
        calendar=_calendar(),
        current_projection=projections["0924"],
        prior_projections={"0920": projections["0920"]},
        trade_date=TRADE_DATE,
        symbol="000001",
        node_tag="0924",
        observed_at=_dt(observed),
        as_of=_dt(local_datetime_ms(TRADE_DATE, "09:24:10")),
    )
    assert result["preflight_gate"] == "BLOCKED"
    assert result["startup_self_check"]["reasons"] == (
        "projection_source_time_after_0924_cutoff",
    )
