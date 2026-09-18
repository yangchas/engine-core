import importlib.util
import json
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path

import pytest

from engine_core import (
    AUCTION_REFERENCE_FUNCTION_ORDER,
    AuctionReferencePreparation,
    DataResult,
    DataStatus,
    FreshnessPolicy,
    RedisQ2ProjectionAdapter,
    build_a_share_session_plan,
    build_calendar_snapshot,
    build_q2_projection,
    canonical_json,
    read_redis_auction_projection,
)
from engine_core.theme_auction_delta_strategy import build_legacy_theme_delta_shadow_trace


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


def _session_contract(trade_date: str):
    calendar = build_calendar_snapshot(
        (trade_date,),
        version="continuous-session-test-v1",
        declared_valid_from=trade_date,
        declared_valid_to=trade_date,
        source_guard_valid_from=trade_date,
        source_guard_valid_to=trade_date,
    )
    return calendar, build_a_share_session_plan(trade_date, calendar)


def _reference_preparation(trade_date: str, knowledge_as_of_ms: int, *, available=True):
    previous_trade_date = (
        datetime.fromisoformat(trade_date).date() - timedelta(days=1)
    ).isoformat()
    results = []
    for function_id in AUCTION_REFERENCE_FUNCTION_ORDER:
        results.append(
            (
                function_id,
                DataResult(
                    request_id=f"test:{trade_date}:{function_id}",
                    function_id=function_id,
                    status=DataStatus.READY,
                    data={"function_id": function_id},
                    actual_source="fixture",
                    requested_trade_date=trade_date,
                    actual_trade_date=previous_trade_date,
                    effective_at_ms=knowledge_as_of_ms - 1,
                    available_at_ms=(knowledge_as_of_ms - 1 if available else None),
                    observed_at_ms=knowledge_as_of_ms - 1,
                    schema_version=1,
                    completeness=1.0,
                ),
            )
        )
    return AuctionReferencePreparation(
        trade_date=trade_date,
        previous_trade_date=previous_trade_date,
        knowledge_as_of_ms=knowledge_as_of_ms,
        results=tuple(results),
    )


def _theme_delta_result(trade_date: str, knowledge_as_of_ms: int, *, status=DataStatus.READY):
    return DataResult(
        request_id=f"test:{trade_date}:theme_auction_delta_compat",
        function_id="theme_auction_delta_compat",
        status=status,
        data={
            "facts": (
                {
                    "theme_id": "theme-a",
                    "symbol_count": 1,
                    "amount_0925": 100.0,
                    "amount_delta_24_25": 60_000_000.0,
                    "amount_ratio_avg": 2.0,
                    "bid_amount_delta_24_25": 0.0,
                    "change_pct_delta_avg": 1.0,
                    "positive_delta_count": 1,
                    "evidence_refs": ("fixture://theme/a",),
                },
            )
            if status is not DataStatus.UNAVAILABLE
            else (),
        },
        actual_source="fixture" if status is not DataStatus.UNAVAILABLE else None,
        requested_trade_date=trade_date,
        actual_trade_date=trade_date if status is not DataStatus.UNAVAILABLE else None,
        effective_at_ms=knowledge_as_of_ms - 1 if status is not DataStatus.UNAVAILABLE else None,
        available_at_ms=knowledge_as_of_ms - 1 if status is not DataStatus.UNAVAILABLE else None,
        observed_at_ms=knowledge_as_of_ms - 1,
        schema_version=1,
        completeness=1.0 if status is DataStatus.READY else 0.0,
    )


def _run_continuous_with_theme(fixture: dict, theme_result: DataResult):
    trade_date = fixture["trade_date"]
    evaluation_times = _evaluation_times(trade_date)
    calendar, session_plan = _session_contract(trade_date)
    return MODULE.run_continuous_session_shadow(
        auction_rows=_fixture_rows_without_final_anchor(fixture),
        opening_projection=_q2_projection(trade_date, "09:32:00", "09:32:00"),
        trade_date=trade_date,
        symbol="600519",
        calendar=calendar,
        session_plan=session_plan,
        preparation=_reference_preparation(trade_date, evaluation_times["0925"] - 1_000),
        theme_delta_result=theme_result,
        evaluation_times_ms=evaluation_times,
    )


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
    calendar, session_plan = _session_contract(fixture["trade_date"])
    result = MODULE.run_continuous_session_shadow(
        auction_rows=rows,
        opening_projection=projection,
        trade_date=fixture["trade_date"],
        symbol="600519",
        calendar=calendar,
        session_plan=session_plan,
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


def test_continuous_shadow_binds_one_prefetched_reference_bundle():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    trade_date = fixture["trade_date"]
    evaluation_times = _evaluation_times(trade_date)
    calendar, session_plan = _session_contract(trade_date)
    preparation = _reference_preparation(
        trade_date,
        evaluation_times["0925"] - 1_000,
    )
    result = MODULE.run_continuous_session_shadow(
        auction_rows=_fixture_rows_without_final_anchor(fixture),
        opening_projection=_q2_projection(trade_date, "09:32:00", "09:32:00"),
        trade_date=trade_date,
        symbol="600519",
        calendar=calendar,
        session_plan=session_plan,
        preparation=preparation,
        evaluation_times_ms=evaluation_times,
    )
    assert result["reference_bundle_hash"]
    assert result["processed_signals"] == 9
    assert result["coordinator"]["completed_timer_ids"] == (
        "AUCTION_0920",
        "AUCTION_0924",
        "AUCTION_0925",
        "OPENING_0932",
    )
    assert len(result["coordinator"]["timer_firings"]) == 4


def test_continuous_shadow_preserves_overdue_timer_identity_across_nodes():
    """A single poll may dispatch multiple timers; later nodes reuse identity."""

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    trade_date = fixture["trade_date"]
    evaluation_times = _evaluation_times(trade_date)
    # The first evaluation happens after both 09:20 and 09:24 business
    # anchors.  The coordinator may therefore return both firings together;
    # the second node must retain its original dispatch time while using its
    # own explicit evaluation/cutoff time.
    evaluation_times["0920"] = evaluation_times["0924"]
    evaluation_times["0924"] += 1_000
    calendar, session_plan = _session_contract(trade_date)
    result = MODULE.run_continuous_session_shadow(
        auction_rows=_fixture_rows_without_final_anchor(fixture),
        opening_projection=_q2_projection(trade_date, "09:32:00", "09:32:00"),
        trade_date=trade_date,
        symbol="600519",
        calendar=calendar,
        session_plan=session_plan,
        evaluation_times_ms=evaluation_times,
    )
    assert result["processed_signals"] == 8
    assert result["coordinator"]["completed_timer_ids"] == (
        "AUCTION_0920",
        "AUCTION_0924",
        "AUCTION_0925",
        "OPENING_0932",
    )
    firings = {
        item["timer_id"]: item for item in result["coordinator"]["timer_firings"]
    }
    assert firings["AUCTION_0920"]["fired_time_ms"] == evaluation_times["0920"]
    assert firings["AUCTION_0924"]["fired_time_ms"] == evaluation_times["0920"]


def test_continuous_shadow_rejects_late_reference_preparation():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    trade_date = fixture["trade_date"]
    evaluation_times = _evaluation_times(trade_date)
    calendar, session_plan = _session_contract(trade_date)
    with pytest.raises(ValueError, match="after 0925 evaluation"):
        MODULE.run_continuous_session_shadow(
            auction_rows=_fixture_rows_without_final_anchor(fixture),
            opening_projection=_q2_projection(trade_date, "09:32:00", "09:32:00"),
            trade_date=trade_date,
            symbol="600519",
            calendar=calendar,
            session_plan=session_plan,
            preparation=_reference_preparation(
                trade_date,
                evaluation_times["0925"] + 1,
            ),
            evaluation_times_ms=evaluation_times,
        )


def test_continuous_shadow_rejects_unknown_reference_availability():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    trade_date = fixture["trade_date"]
    evaluation_times = _evaluation_times(trade_date)
    calendar, session_plan = _session_contract(trade_date)
    with pytest.raises(ValueError, match="unknown available_at"):
        MODULE.run_continuous_session_shadow(
            auction_rows=_fixture_rows_without_final_anchor(fixture),
            opening_projection=_q2_projection(trade_date, "09:32:00", "09:32:00"),
            trade_date=trade_date,
            symbol="600519",
            calendar=calendar,
            session_plan=session_plan,
            preparation=_reference_preparation(
                trade_date,
                evaluation_times["0925"] - 1_000,
                available=False,
            ),
            evaluation_times_ms=evaluation_times,
        )


def test_continuous_theme_matches_direct_trace_and_runs_once_at_0925():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    trade_date = fixture["trade_date"]
    evaluation_times = _evaluation_times(trade_date)
    theme_result = _theme_delta_result(trade_date, evaluation_times["0925"] - 1_000)

    direct = build_legacy_theme_delta_shadow_trace(theme_result.data["facts"])
    result = _run_continuous_with_theme(fixture, theme_result)
    auction_results = [
        item
        for item in result["strategy_results"]
        if item["delegated_strategy_id"] == "auction-shadow-v1"
    ]
    theme_results = [
        item["child_trace"]["theme_delta_shadow"]
        for item in auction_results
        if "theme_delta_shadow" in item["child_trace"]
    ]
    assert len(theme_results) == 1
    assert theme_results[0]["function_id"] == "theme_auction_delta_compat"
    assert canonical_json(theme_results[0]["shadow"]) == canonical_json(direct)
    assert theme_results[0]["shadow"]["signal_counts"] == {"增量转强": 1}
    assert result["pending_evaluations"] == ()


def test_continuous_theme_unavailable_is_not_promoted():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    trade_date = fixture["trade_date"]
    evaluation_times = _evaluation_times(trade_date)
    result = _run_continuous_with_theme(
        fixture,
        _theme_delta_result(
            trade_date,
            evaluation_times["0925"] - 1_000,
            status=DataStatus.UNAVAILABLE,
        ),
    )
    final_auction = next(
        item
        for item in result["strategy_results"]
        if item["trigger_id"] == "AUCTION_0925"
    )
    theme_shadow = final_auction["child_trace"]["theme_delta_shadow"]
    assert theme_shadow["data_status"] == DataStatus.UNAVAILABLE.value
    assert theme_shadow["shadow"] is None
    assert theme_shadow["reason_codes"] == ["THEME_DATA_NOT_READY"]


def test_continuous_theme_only_binds_without_reference_prefetch():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    trade_date = fixture["trade_date"]
    evaluation_times = _evaluation_times(trade_date)
    calendar, session_plan = _session_contract(trade_date)
    result = MODULE.run_continuous_session_shadow(
        auction_rows=_fixture_rows_without_final_anchor(fixture),
        opening_projection=_q2_projection(trade_date, "09:32:00", "09:32:00"),
        trade_date=trade_date,
        symbol="600519",
        calendar=calendar,
        session_plan=session_plan,
        theme_delta_result=_theme_delta_result(
            trade_date,
            evaluation_times["0925"] - 1_000,
        ),
        evaluation_times_ms=evaluation_times,
    )
    assert result["processed_signals"] == 9
    assert result["pending_evaluations"] == ()
    final_auction = next(
        item
        for item in result["strategy_results"]
        if item["trigger_id"] == "AUCTION_0925"
    )
    assert final_auction["child_trace"]["theme_delta_shadow"]["shadow"]["signal_counts"] == {
        "增量转强": 1
    }


def test_continuous_theme_rejects_wrong_function_identity():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    trade_date = fixture["trade_date"]
    evaluation_times = _evaluation_times(trade_date)
    wrong = DataResult(
        request_id="wrong-theme",
        function_id="other_function",
        status=DataStatus.UNAVAILABLE,
        data=None,
        actual_source=None,
        requested_trade_date=trade_date,
        actual_trade_date=None,
        effective_at_ms=None,
        available_at_ms=None,
        observed_at_ms=evaluation_times["0925"] - 1,
        schema_version=1,
        completeness=0.0,
    )
    with pytest.raises(ValueError, match="function_id"):
        _run_continuous_with_theme(fixture, wrong)


def test_continuous_theme_rejects_future_availability():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    trade_date = fixture["trade_date"]
    evaluation_times = _evaluation_times(trade_date)
    future = replace(
        _theme_delta_result(trade_date, evaluation_times["0925"] - 1_000),
        available_at_ms=evaluation_times["0925"] + 1,
    )
    with pytest.raises(ValueError, match="after knowledge cutoff"):
        _run_continuous_with_theme(fixture, future)


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
    calendar, session_plan = _session_contract(fixture["trade_date"])
    with pytest.raises(ValueError, match="trade_date"):
        MODULE.run_continuous_session_shadow(
            auction_rows=rows,
            opening_projection=_q2_projection("2026-09-02", "09:32:00", "09:32:00"),
            trade_date=fixture["trade_date"],
            symbol="600519",
            calendar=calendar,
            session_plan=session_plan,
            evaluation_times_ms=_evaluation_times(fixture["trade_date"]),
        )


def test_continuous_shadow_rejects_future_opening_source_time():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    calendar, session_plan = _session_contract(fixture["trade_date"])
    with pytest.raises(ValueError, match="source time"):
        MODULE.run_continuous_session_shadow(
            auction_rows=_fixture_rows_without_final_anchor(fixture),
            opening_projection=_q2_projection(
                fixture["trade_date"], "09:32:01", "09:32:00"
            ),
            trade_date=fixture["trade_date"],
            symbol="600519",
            calendar=calendar,
            session_plan=session_plan,
            evaluation_times_ms=_evaluation_times(fixture["trade_date"]),
        )


def test_continuous_shadow_rejects_auction_source_after_explicit_evaluation_time():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    calendar, session_plan = _session_contract(fixture["trade_date"])
    rows = _fixture_rows_without_final_anchor(fixture)
    for row in rows:
        if row.get("auction_tag") == "0925":
            row["ts"] += timedelta(seconds=1)
    with pytest.raises(ValueError, match="after node cutoff"):
        MODULE.run_continuous_session_shadow(
            auction_rows=rows,
            opening_projection=_q2_projection(
                fixture["trade_date"], "09:32:00", "09:32:00"
            ),
            trade_date=fixture["trade_date"],
            symbol="600519",
            calendar=calendar,
            session_plan=session_plan,
            evaluation_times_ms=_evaluation_times(fixture["trade_date"]),
        )


def test_continuous_shadow_rejects_evaluation_times_on_another_trade_date():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    calendar, session_plan = _session_contract(fixture["trade_date"])
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
            calendar=calendar,
            session_plan=session_plan,
            evaluation_times_ms=evaluation_times,
        )


def test_continuous_shadow_rejects_evaluation_before_0925_business_anchor():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    calendar, session_plan = _session_contract(fixture["trade_date"])
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
            calendar=calendar,
            session_plan=session_plan,
            evaluation_times_ms=evaluation_times,
        )


def test_continuous_shadow_rejects_0925_before_six_second_settling_barrier():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    calendar, session_plan = _session_contract(fixture["trade_date"])
    with pytest.raises(ValueError, match="finalization barrier 09:25:06"):
        MODULE.run_continuous_session_shadow(
            auction_rows=_fixture_rows_without_final_anchor(fixture),
            opening_projection=_q2_projection(fixture["trade_date"], "09:32:00", "09:32:00"),
            trade_date=fixture["trade_date"],
            symbol=fixture["symbol"],
            calendar=calendar,
            session_plan=session_plan,
            evaluation_times_ms=_evaluation_times(fixture["trade_date"], auction_0925="09:25:05"),
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
    calendar, session_plan = _session_contract(trade_date)
    result = MODULE.run_continuous_redis_session_shadow(
        auction_projections=auction,
        opening_projection=opening,
        trade_date=trade_date,
        symbol="600519",
        calendar=calendar,
        session_plan=session_plan,
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
    calendar, session_plan = _session_contract(trade_date)
    duplicate = tuple(auction) + (auction[0],)
    try:
        MODULE.run_continuous_redis_session_shadow(
            auction_projections=duplicate,
            opening_projection=opening,
            trade_date=trade_date,
            symbol="600519",
            calendar=calendar,
            session_plan=session_plan,
            evaluation_times_ms=_evaluation_times(trade_date, auction_0925="09:25:06"),
        )
    except ValueError as exc:
        assert "duplicate Redis auction tag" in str(exc)
    else:
        raise AssertionError("duplicate Redis auction tag was accepted")
