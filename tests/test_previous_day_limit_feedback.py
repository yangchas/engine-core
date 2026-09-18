from __future__ import annotations

from engine_core import (
    DataResult,
    DataStatus,
    FactStatus,
    build_previous_day_limit_feedback,
)
from engine_core.contracts import Provenance


def _previous_result(*, status=DataStatus.READY, rows=None, available_at_ms=1_000):
    rows = rows or [
        {
            "trade_date": "2026-09-17",
            "symbol": "600519",
            "name": "贵州茅台",
            "lb_days": 3,
            "plate": "白酒",
        },
        {
            "trade_date": "2026-09-17",
            "symbol": "000001",
            "name": "平安银行",
            "lb_days": 1,
            "plate": "银行",
        },
    ]
    return DataResult(
        request_id="previous-limit-feedback-test",
        function_id="previous_day_limit_pool",
        status=status,
        data={"previous_trade_date": "2026-09-17", "rows": rows},
        actual_source="fixture-kaipan",
        requested_trade_date="2026-09-18",
        actual_trade_date="2026-09-17",
        effective_at_ms=1_000,
        available_at_ms=available_at_ms,
        observed_at_ms=1_000,
        schema_version=1,
        completeness=1.0 if status is DataStatus.READY else 0.5,
        provenance=(
            Provenance(
                source_id="fixture-kaipan",
                source_kind="fixture",
                source_schema="PreviousDayLimitPoolV1",
                source_trade_date="2026-09-17",
                effective_at_ms=1_000,
                observed_at_ms=1_000,
                evidence_ref="fixture://previous-limit/20260917",
            ),
        ),
    )


def _current_rows():
    return [
        {
            "trade_date": "2026-09-18",
            "tag": "0925",
            "symbol": "600519",
            "change_pct": 2.5,
            "auction_amount_yuan": 8_000_000,
            "source_record_time_ms": 1_002,
        },
        {
            "trade_date": "2026-09-18",
            "tag": "0925",
            "symbol": "000001",
            "change_pct": -1.0,
            "auction_amount_yuan": 3_000_000,
            "source_record_time_ms": 1_003,
        },
    ]


def test_feedback_derives_objective_return_counts_and_structure():
    fact = build_previous_day_limit_feedback(
        _previous_result(),
        _current_rows(),
        current_trade_date="2026-09-18",
    )

    assert fact.status is FactStatus.READY
    assert fact.prior_limit_up_count == 2
    assert fact.valid_return_count == 2
    assert fact.return_unavailable_count == 0
    assert fact.up_count == 1
    assert fact.down_count == 1
    assert fact.flat_count == 0
    assert fact.up_ratio == 0.5
    assert fact.median_return_pct == 0.75
    assert fact.highest_board_height == 3
    assert fact.highest_board_symbols == ("600519",)
    assert fact.source_time_min_ms == 1_002
    assert fact.source_time_max_ms == 1_003
    assert fact.records[0]["current_change_pct"] == 2.5


def test_feedback_does_not_turn_missing_current_return_into_zero():
    rows = _current_rows()
    rows[1] = {**rows[1], "change_pct": None}
    fact = build_previous_day_limit_feedback(
        _previous_result(),
        rows,
        current_trade_date="2026-09-18",
    )

    assert fact.status is FactStatus.PARTIAL
    assert fact.valid_return_count == 1
    assert fact.return_unavailable_count == 1
    assert fact.down_count == 0
    assert "change_pct:000001" in fact.missing_fields
    assert fact.records[1]["current_change_pct"] is None


def test_feedback_does_not_claim_ready_without_source_time_evidence():
    rows = [{key: value for key, value in row.items() if key != "source_record_time_ms"} for row in _current_rows()]
    fact = build_previous_day_limit_feedback(
        _previous_result(),
        rows,
        current_trade_date="2026-09-18",
    )
    assert fact.status is FactStatus.PARTIAL
    assert fact.source_time_min_ms is None
    assert fact.source_time_max_ms is None
    assert "source_record_time_ms:600519" in fact.missing_fields
    assert "source_record_time_ms:000001" in fact.missing_fields


def test_feedback_requires_explicit_0925_and_trade_date():
    rows = [{**_current_rows()[0], "tag": "0924"}]
    fact = build_previous_day_limit_feedback(
        _previous_result(),
        rows,
        current_trade_date="2026-09-18",
    )
    assert fact.status is FactStatus.UNAVAILABLE
    assert fact.invalid_fields == ("current[0].tag",)


def test_feedback_duplicate_current_symbol_is_invalid_not_last_row_wins():
    rows = _current_rows() + [{**_current_rows()[0], "change_pct": 9.0}]
    fact = build_previous_day_limit_feedback(
        _previous_result(),
        rows,
        current_trade_date="2026-09-18",
    )
    assert fact.status is FactStatus.UNAVAILABLE
    assert fact.invalid_fields == ("current[2].symbol_duplicate",)


def test_feedback_unavailable_previous_result_never_leaks_rows():
    fact = build_previous_day_limit_feedback(
        _previous_result(status=DataStatus.UNAVAILABLE, available_at_ms=None),
        _current_rows(),
        current_trade_date="2026-09-18",
    )
    assert fact.status is FactStatus.UNAVAILABLE
    assert fact.records == ()
    assert fact.prior_limit_up_count is None


def test_feedback_requires_current_trade_date_to_match_rows():
    rows = [{**_current_rows()[0], "trade_date": "2026-09-17"}]
    fact = build_previous_day_limit_feedback(
        _previous_result(),
        rows,
        current_trade_date="2026-09-18",
    )
    assert fact.status is FactStatus.UNAVAILABLE
    assert fact.invalid_fields == ("current[0].trade_date",)


def test_feedback_hash_excludes_provider_evidence_identity():
    left = build_previous_day_limit_feedback(
        _previous_result(), _current_rows(), current_trade_date="2026-09-18"
    )
    right_result = _previous_result()
    right_result = DataResult(
        **{
            field: getattr(right_result, field)
            for field in (
                "request_id", "function_id", "status", "data", "actual_source",
                "requested_trade_date", "actual_trade_date", "effective_at_ms",
                "available_at_ms", "observed_at_ms", "schema_version", "completeness",
                "missing_fields", "missing_symbols", "temporal_mode", "fetch_completed_at_ms",
            )
        },
        provenance=(
            Provenance(
                source_id="other-provider",
                source_kind="fixture",
                source_schema="PreviousDayLimitPoolV1",
                source_trade_date="2026-09-17",
                effective_at_ms=1_000,
                observed_at_ms=1_000,
                evidence_ref="fixture://other",
            ),
        ),
    )
    right = build_previous_day_limit_feedback(
        right_result, _current_rows(), current_trade_date="2026-09-18"
    )
    assert left.content_hash == right.content_hash
    assert left.evidence_hash != right.evidence_hash


def test_feedback_evidence_hash_tracks_current_source_time_range():
    left = build_previous_day_limit_feedback(
        _previous_result(), _current_rows(), current_trade_date="2026-09-18"
    )
    shifted = [
        {**row, "source_record_time_ms": row["source_record_time_ms"] + 100}
        for row in _current_rows()
    ]
    right = build_previous_day_limit_feedback(
        _previous_result(), shifted, current_trade_date="2026-09-18"
    )
    assert left.content_hash != right.content_hash
    assert left.evidence_hash != right.evidence_hash
