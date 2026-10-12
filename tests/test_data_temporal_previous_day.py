from engine_core import DataRequest, DataStatus, TemporalDataGuard
from engine_core.contracts import DataResult


def _request(allow=False):
    return DataRequest("r", "previous_day_stats", "2026-09-04", 1788484800000,
                       1788484800000, allow_unverified_previous_day_availability=allow)


def _result(actual_trade_date="2026-09-03"):
    return DataResult("r", "previous_day_stats", DataStatus.READY, {"value": 1},
                      "fixture", "2026-09-04", actual_trade_date, None, None,
                      1788484800000, 1, 0.75)


def test_unknown_previous_day_availability_remains_unavailable_by_default():
    guarded = TemporalDataGuard.check(_result(), _request())

    assert guarded.status is DataStatus.UNAVAILABLE
    assert "available_at_unknown" in guarded.missing_fields


def test_opted_in_previous_day_data_is_partial_and_marked():
    guarded = TemporalDataGuard.check(_result(), _request(allow=True))

    assert guarded.status is DataStatus.PARTIAL
    assert guarded.completeness == 0.75
    marker = [item for item in guarded.provenance if item.source_kind == "temporal_guard"]
    assert len(marker) == 1
    assert marker[0].notes == (
        "available_at_unknown_previous_day_data",
        "not_historical_availability_evidence",
    )


def test_opted_in_unknown_availability_does_not_allow_same_day_data():
    guarded = TemporalDataGuard.check(_result("2026-09-04"), _request(True))

    assert guarded.status is DataStatus.UNAVAILABLE
    assert "available_at_unknown" in guarded.missing_fields
