from __future__ import annotations

from datetime import datetime, timezone

from engine_core import canonical_previous_day_limit_pool_payload_hash
from examples.run_real_previous_day_limit_pool import run_real_previous_day_limit_pool


def _rows():
    return [
        {
            "trade_date": "2026-09-10",
            "symbol": "600519",
            "name": "样本甲",
            "lb_days": 2,
            "plate": "消费",
            "seal_time": "09:31:22",
            "turnover": 61_814_324.0,
            "close_pct": 10.01,
            "source": "kaipan",
        }
    ]


def test_real_composition_keeps_redis_observation_separate_from_core_result():
    result = run_real_previous_day_limit_pool(
        trade_date="2026-09-11",
        previous_trade_date="2026-09-10",
        observed_at=datetime(2026, 9, 11, 1, 0, tzinfo=timezone.utc),
        redis_kwargs={},
        fetch_rows_override=lambda previous: _rows(),
        redis_summary_override={
            "redis_type": "hash",
            "row_count": 1,
            "scan_consistent": True,
            "meta": {"trade_date": "2026-09-10", "source": "kaipan"},
        },
    )
    assert result["result_status"] == "UNAVAILABLE"
    assert result["missing_fields"] == ("available_at_unknown",)
    assert result["redis"]["row_count"] == 1
    assert result["read_only"] is True
    assert result["data"]["scope"] == "provider_declared_pool"
    assert result["data"]["field_units"]["turnover_yuan"] == "yuan"
    assert result["structure_fact"]["status"] == "UNAVAILABLE"
    assert result["structure_fact"]["row_count"] is None


def test_real_composition_accepts_verified_availability_only_at_provider_boundary():
    # The real runner intentionally does not expose an availability override;
    # this test documents that the default real path remains fail-closed.
    result = run_real_previous_day_limit_pool(
        trade_date="2026-09-11",
        previous_trade_date="2026-09-10",
        observed_at=datetime(2026, 9, 11, 1, 0, tzinfo=timezone.utc),
        redis_kwargs={},
        fetch_rows_override=lambda previous: _rows(),
    )
    assert result["available_at_ms"] is None
    assert result["result_status"] == "UNAVAILABLE"


def test_real_composition_preserves_explicit_live_temporal_mode():
    result = run_real_previous_day_limit_pool(
        trade_date="2026-09-11",
        previous_trade_date="2026-09-10",
        observed_at=datetime(2026, 9, 11, 1, 0, tzinfo=timezone.utc),
        redis_kwargs={},
        temporal_mode="LIVE",
        fetch_rows_override=lambda previous: _rows(),
    )
    assert result["temporal_mode"] == "LIVE"
    assert result["fetch_completed_at_ms"] == result["observed_at_ms"]
    # No availability metadata is invented merely because the mode is LIVE.
    assert result["available_at_ms"] is None
    assert result["result_status"] == "READY"


def test_real_composition_accepts_only_explicit_redis_meta_contract():
    result = run_real_previous_day_limit_pool(
        trade_date="2026-09-11",
        previous_trade_date="2026-09-10",
        observed_at=datetime(2026, 9, 11, 1, 0, tzinfo=timezone.utc),
        redis_kwargs={},
        fetch_rows_override=lambda previous: _rows(),
        redis_summary_override={
            "redis_type": "hash",
            "row_count": 1,
            "scan_consistent": True,
            "meta": {
                "trade_date": "2026-09-10",
                "source": "kaipan",
                "schema_version": "PreviousDayLimitPoolV1",
                "available_at_ms": 1789070000000,
                "field_units": {
                    "lb_days": "boards",
                    "turnover_yuan": "yuan",
                    "close_pct": "percent",
                },
                "payload_sha256": canonical_previous_day_limit_pool_payload_hash(_rows()),
            },
        },
    )
    assert result["result_status"] == "READY"
    assert result["available_at_ms"] == 1789070000000
    assert result["data"]["field_units"]["turnover_yuan"] == "yuan"
