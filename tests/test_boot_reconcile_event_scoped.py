# DATA: spec (a health item for the registry's own hot_plates_gaps entry; the registry is the declared config)
"""C665: an event-scoped freshness rule ('expected_by': 'event deadline') has no clock to compare, so a MISSING item
for it is normal (no gap was recorded because no capture window expired) and must never crash boot_reconcile."""
from engine_core.guardian.startup import boot_reconcile
from engine_core.guardian.registry import load_registry


def test_missing_event_scoped_item_is_not_a_gap_and_does_not_crash(tmp_path):
    item = next(i for i in load_registry() if i["id"] == "hot_plates_gaps")
    assert item["freshness"]["expected_by"] == "event deadline"
    health = {"items": [{"id": "hot_plates_gaps", "dataset": "hot_plates_gaps", "status": "MISSING"}]}
    result = boot_reconcile("2026-10-23", item["phases"][-1], health, tmp_path, now="2026-10-23T15:10:00+08:00")
    assert result["status"] in ("ok", "degraded"), result
    assert "hot_plates_gaps" not in {gap["id"] for gap in result["detail"]["gaps"]}
