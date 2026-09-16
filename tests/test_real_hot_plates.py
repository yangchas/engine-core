from __future__ import annotations

from datetime import datetime, timezone

from engine_core import canonical_hot_plates_payload_hash
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
                "payload_sha256": canonical_hot_plates_payload_hash(_rows()),
            },
        },
    )

    assert result["result_status"] == "READY"
    assert result["available_at_ms"] == 1789070000000
    assert result["data"]["field_units"]["net_inflow_yi"] == "yi"


def test_real_composition_accepts_injected_calendar_without_changing_data_contract():
    result = run_real_hot_plates(
        trade_date="2026-09-10",
        observed_at=datetime(2026, 9, 11, 1, 0, tzinfo=timezone.utc),
        redis_kwargs={},
        fetch_rows_override=lambda date: _rows(),
        redis_summary_override={
            "redis_type": "hash",
            "row_count": 1,
            "scan_consistent": True,
            "meta": {"trade_date": "2026-09-10", "source": "kaipan"},
        },
        calendar_trading_dates=("2026-09-09", "2026-09-10", "2026-09-11"),
        calendar_source_id="baostock",
        calendar_evidence_ref="calendar://test/20260913",
    )

    assert result["result_status"] == "UNAVAILABLE"
    assert result["data"]["trade_date"] == "2026-09-10"
    calendar_provenance = next(
        item for item in result["provenance"] if item["source_kind"] == "calendar"
    )
    assert calendar_provenance["source_id"] == "CN_A_SHARE"
    assert calendar_provenance["evidence_ref"] == "calendar://test/20260913"
    assert "calendar_version=real-hot-plates-probe-2026-09-10" in calendar_provenance["notes"]


def test_real_composition_rejects_trade_date_outside_injected_calendar():
    try:
        run_real_hot_plates(
            trade_date="2026-09-10",
            observed_at=datetime(2026, 9, 11, 1, 0, tzinfo=timezone.utc),
            redis_kwargs={},
            fetch_rows_override=lambda date: _rows(),
            redis_summary_override={"redis_type": "hash", "row_count": 1, "scan_consistent": True},
            calendar_trading_dates=("2026-09-09",),
        )
    except ValueError as exc:
        assert "absent from supplied calendar" in str(exc)
    else:
        raise AssertionError("expected injected calendar membership validation")
