from __future__ import annotations

from datetime import datetime, timezone

from examples.run_real_hot_plates import run_real_hot_plates


def _rows():
    return [
        {
            "trade_date": "2026-09-10",
            "plate_name": "机器人",
            "rank": 1,
            "strength": 200.0,
            "hot": 200.0,
            "change_pct": 1.0,
            "net_inflow_yi": 2.5,
            "source": "kaipan",
        }
    ]


def test_real_composition_keeps_legacy_meta_fail_closed():
    result = run_real_hot_plates(
        trade_date="2026-09-10",
        observed_at=datetime(2026, 9, 11, 1, 0, tzinfo=timezone.utc),
        redis_kwargs={},
        fetch_rows_override=lambda date: _rows(),
        redis_summary_override={
            "redis_type": "hash",
            "row_count": 1,
            "scan_consistent": True,
            "meta": {
                "trade_date": "2026-09-10",
                "source": "kaipan",
                "updated_at": "2026-09-10 17:40:06",
            },
        },
    )

    assert result["result_status"] == "UNAVAILABLE"
    assert result["missing_fields"] == ("available_at_unknown",)
    assert result["redis"]["row_count"] == 1
    assert result["read_only"] is True
    assert result["data"]["scope"] == "provider_declared_top_plates"


def test_real_composition_accepts_only_explicit_hot_plate_metadata():
    result = run_real_hot_plates(
        trade_date="2026-09-10",
        observed_at=datetime(2026, 9, 11, 1, 0, tzinfo=timezone.utc),
        redis_kwargs={},
        fetch_rows_override=lambda date: _rows(),
        redis_summary_override={
            "redis_type": "hash",
            "row_count": 1,
            "scan_consistent": True,
            "meta": {
                "trade_date": "2026-09-10",
                "source": "kaipan",
                "schema_version": "HotPlatesV1",
                "available_at_ms": 1789070000000,
                "field_units": {
                    "rank": "ordinal",
                    "strength": "score",
                    "hot": "score",
                    "change_pct": "percent",
                    "net_inflow_yi": "yi",
                },
            },
        },
    )

    assert result["result_status"] == "READY"
    assert result["available_at_ms"] == 1789070000000
    assert result["data"]["field_units"]["net_inflow_yi"] == "yi"
