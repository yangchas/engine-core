"""Fetch postconditions using the C887 recorded TD ACTIVE snapshot."""
import json
from pathlib import Path

import pytest

from engine_core.data_pipeline.reference_snapshot_td import ReferenceSnapshotStore
from engine_core.guardian import startup


@pytest.mark.parametrize("snapshot_state,asof,expected", [
    ("activated", "2026-11-04T08:00:00+08:00", "ok"),
    ("missing", "2026-11-04T08:00:00+08:00", "failed"),
    ("activated", "2026-11-04T00:00:00+08:00", "failed"),
])
def test_fetch_plate_members_checks_activated_snapshot(tmp_path, monkeypatch, snapshot_state, asof, expected):
    snapshot = json.loads((Path(__file__).parent / "fixtures/real/td/outputs/reference_snapshot_plate_members_latest.json").read_text())["value"]
    state = {"snapshot": None}
    (tmp_path / "catalog.json").write_text(json.dumps({"datasets": {}}))
    monkeypatch.setattr(ReferenceSnapshotStore, "read_latest", lambda self, dataset: state["snapshot"])
    monkeypatch.setattr(ReferenceSnapshotStore, "close", lambda self: None)

    def fill(item, day, timeout, root):
        assert day == "2026-11-04"
        if snapshot_state == "activated":
            state["snapshot"] = snapshot
        return {"status": "READY"}

    monkeypatch.setattr(startup, "_run_fetch_job", fill)
    result = startup.fetch_item("plate_members", "2026-11-04", tmp_path, dry_run=False, now=asof)
    assert result["status"] == expected, result
    if expected == "failed":
        assert result["error_code"] == "POSTCONDITION_FAILED"
        assert "partition_missing_after_fetch:plate_members:2026-11-04" in result["detail"]["error"]
