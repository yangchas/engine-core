from datetime import datetime, timezone

import pytest

from engine_core.guardian.registry import (
    PHASES,
    load_registry,
    stale_items,
    validate_registry,
)


def test_registry_schema_and_phases():
    registry = load_registry()
    assert registry
    assert validate_registry(registry) > 0  # unresolved fields stay visible
    assert all(set(item["phases"]) <= PHASES for item in registry)
    assert all({"id", "owner", "artifact", "phases", "cadence", "rule",
                "freshness", "retry", "degrade", "lifecycle", "notes", "guardian_event"} <= item.keys()
               for item in registry)


def test_invalid_phase_is_rejected():
    with pytest.raises(ValueError, match="phase"):
        validate_registry([{"id": "x", "owner": None, "artifact": "x",
                            "phases": ["NOT_A_PHASE"], "cadence": "UNKNOWN",
                            "rule": "UNKNOWN", "freshness": "UNKNOWN",
                            "retry": "UNKNOWN", "degrade": [],
                            "lifecycle": "UNKNOWN", "notes": "UNKNOWN", "guardian_event": None}])


def test_real_catalog_freshness_smoke():
    items = load_registry()
    stale = stale_items(datetime.now(timezone.utc), "/home/exedev/core-data/catalog.json")
    assert isinstance(stale, list)
    assert any(item["id"] == "daily_kline" for item in items)
    assert all("status" in item and "id" in item for item in stale)


def test_partition_freshness_states():
    from datetime import timedelta
    tz = timezone(timedelta(hours=8))
    items = load_registry()
    catalog = {"datasets": {"daily_kline": {"dates": {"2026-10-19": [{}]}},
                            "limit_up_pool": {"dates": {"2026-10-19": [{}]}}}}
    early = datetime(2026, 10, 20, 8, 0, tzinfo=tz)
    late = datetime(2026, 10, 20, 19, 0, tzinfo=tz)
    by = lambda rows: {r["id"]: r["status"] for r in rows}
    pending = by(stale_items(early, catalog, items, trade_date="2026-10-20", prev_trade_date="2026-10-19"))
    overdue = by(stale_items(late, catalog, items, trade_date="2026-10-20", prev_trade_date="2026-10-19"))
    assert pending["daily_kline"] == "PENDING" and overdue["daily_kline"] == "OVERDUE"
    assert pending["limit_up_pool"] == overdue["limit_up_pool"] == "OK"
