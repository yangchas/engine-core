from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from engine_core import (
    DataContext,
    DataRequest,
    DataStatus,
    FactStatus,
    PreviousDayLimitPoolFunction,
    PreviousDayLimitStructureFact,
    RedisPreviousDayLimitPoolProvider,
    build_calendar_snapshot,
    build_previous_day_limit_structure,
)


CALENDAR = build_calendar_snapshot(
    ["2026-09-09", "2026-09-10", "2026-09-11"],
    version="fixture-structure-v1",
    declared_valid_from="2026-09-09",
    declared_valid_to="2026-09-11",
    source_guard_valid_from="2026-09-09",
    source_guard_valid_to="2026-09-13",
    source_id="fixture-calendar",
    observed_at_ms=1789000000000,
)


def _rows():
    return [
        {
            "trade_date": "2026-09-10", "symbol": "600519", "name": "样本甲",
            "lb_days": 2, "plate": "消费", "seal_time": "09:31:22",
            "turnover": 61_814_324.0, "close_pct": 10.01, "source": "kaipan",
        },
        {
            "trade_date": "2026-09-10", "symbol": "000001", "name": "样本乙",
            "lb_days": 1, "plate": None, "seal_time": None,
            "turnover": 101_359_868.0, "close_pct": 9.9, "source": "kaipan",
        },
    ]


def _request():
    return DataRequest(
        request_id="structure-test",
        function_id="previous_day_limit_pool",
        trade_date="2026-09-11",
        effective_as_of_ms=1789080000000,
        knowledge_as_of_ms=1789080000000,
    )


def _result(status=DataStatus.READY):
    provider = RedisPreviousDayLimitPoolProvider(
        lambda previous: _rows(),
        observed_at_ms=lambda: 1789080000000,
        available_at_ms=lambda: 1789070000000,
        verified_field_units={"turnover_yuan": "yuan"},
    )
    result = PreviousDayLimitPoolFunction(provider, CALENDAR).execute(
        DataContext("structure-test", "READ_ONLY", 1789080000000), _request()
    )
    return result if status is DataStatus.READY else replace(result, status=status)


def test_structure_fact_derives_only_previous_session_board_shape():
    fact = build_previous_day_limit_structure(_result())
    assert isinstance(fact, PreviousDayLimitStructureFact)
    assert fact.status.value == "READY"
    assert fact.previous_trade_date == "2026-09-10"
    assert fact.row_count == 2
    assert fact.highest_board_height == 2
    assert fact.highest_board_symbols == ("600519",)
    assert fact.board_height_distribution == {"1": 1, "2": 1}
    assert fact.content_hash and fact.evidence_hash


def test_unavailable_source_never_leaks_rows_into_runtime_fact():
    fact = build_previous_day_limit_structure(_result(DataStatus.UNAVAILABLE))
    assert fact.status.value == "UNAVAILABLE"
    assert fact.row_count is None
    assert fact.highest_board_height is None
    assert fact.board_height_distribution == {}


def test_structure_semantic_hash_excludes_provider_evidence_identity():
    left = build_previous_day_limit_structure(_result())
    right = build_previous_day_limit_structure(
        replace(_result(), actual_source="redis-other")
    )
    assert left.content_hash == right.content_hash
    assert left.evidence_hash != right.evidence_hash


def test_real_redis_capture_is_used_but_unknown_availability_stays_fail_closed():
    payload = json.loads(
        (Path(__file__).parent / "fixtures/data/previous_day_limit_pool_20260917_real.json")
        .read_text(encoding="utf-8")
    )
    real_calendar = build_calendar_snapshot(
        ["2026-09-17", "2026-09-18"],
        version="real-structure-20260918",
        declared_valid_from="2026-09-18",
        declared_valid_to="2026-09-18",
        source_guard_valid_from="2026-09-17",
        source_guard_valid_to="2026-09-18",
        source_id="real-capture-calendar",
        observed_at_ms=payload["observed_at_ms"],
    )
    raw_rows = [
        {
            **row,
            "turnover": row["turnover_yuan"],
        }
        for row in payload["data"]["rows"]
    ]
    for row in raw_rows:
        row.pop("turnover_yuan", None)
    provider = RedisPreviousDayLimitPoolProvider(
        lambda previous: raw_rows,
        observed_at_ms=lambda: payload["observed_at_ms"],
        metadata=lambda previous: payload["redis"]["meta"],
        evidence_ref="fixture://real-redis/2026-09-17",
    )
    result = PreviousDayLimitPoolFunction(provider, real_calendar).execute(
        DataContext("real-structure", "READ_ONLY", payload["observed_at_ms"]),
        DataRequest(
            request_id="real-structure-20260918",
            function_id="previous_day_limit_pool",
            trade_date="2026-09-18",
            effective_as_of_ms=payload["observed_at_ms"],
            knowledge_as_of_ms=payload["observed_at_ms"],
        ),
    )
    assert result.status is DataStatus.UNAVAILABLE
    assert result.actual_trade_date == "2026-09-17"
    assert result.data["row_count"] == 47
    fact = build_previous_day_limit_structure(result)
    assert fact.status is FactStatus.UNAVAILABLE
    assert fact.row_count is None
    assert fact.highest_board_height is None
