from __future__ import annotations

import sys
import json
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))

import rehearse_event as rehearsal  # noqa: E402


TZ = ZoneInfo("Asia/Shanghai")


def real_schedule() -> dict:
    return rehearsal.load_schedule(ROOT / "src/engine_core/guardian/schedule.json")


def test_retime_uses_real_limit_pool_schedule_and_clears_phase_limit() -> None:
    schedule = real_schedule()
    now = datetime(2026, 10, 29, 8, 0, 7, tzinfo=TZ)

    prepared, event_ids = rehearsal.prepare_schedule(schedule, "limit_pool_d1", [], "rh123abc", now, 3)

    event = prepared["events"][0]
    assert event["event_id"] == event_ids["limit_pool_d1"]
    assert event["at"] == "08:03:07"
    assert event["deadline"] == "09:03:07"
    assert event["late_until"] == "09:03:07"
    assert "phases" not in event
    assert event["gate"] == "calendar_ready"


def test_filter_preserves_and_remaps_real_dependency_edges() -> None:
    schedule = real_schedule()

    prepared, event_ids = rehearsal.prepare_schedule(
        schedule, "limit_reasons_d1", ["limit_pool_d1"], "rh123abc",
        datetime(2026, 10, 29, 8, 0, 0, tzinfo=TZ), 3,
    )

    assert {event["event_id"] for event in prepared["events"]} == {
        event_ids["limit_reasons_d1"], event_ids["limit_pool_d1"]
    }
    target = next(event for event in prepared["events"] if event["event_id"] == event_ids["limit_reasons_d1"])
    assert target["depends_on"] == [event_ids["limit_pool_d1"]]


def test_state_root_length_limit() -> None:
    rehearsal.validate_state_root_path(Path("/tmp/rh123abc/s"))

    with pytest.raises(ValueError, match="state_root_path_too_long"):
        rehearsal.validate_state_root_path(Path("/tmp/" + "x" * 40))


def test_generated_alert_command_must_stay_dry_run() -> None:
    with tempfile.TemporaryDirectory(prefix="c601-config-") as temporary:
        config_path = Path(temporary) / "config.json"
        subprocess.run(
            [sys.executable, str(ROOT / "native/guardiand/scripts/make_config.py"),
             str(ROOT / "src/engine_core/guardian/schedule.json"), str(config_path), str(ROOT), sys.executable],
            check=True, capture_output=True, text=True,
        )
        config = json.loads(config_path.read_text(encoding="utf-8"))

    assert rehearsal.alert_is_dry_run(config)

    config["handlers"]["alert"]["argv"][-1] = "--email-send"
    assert not rehearsal.alert_is_dry_run(config)


def test_core_data_staging_policy_copies_metadata_and_links_real_partitions() -> None:
    data_root = rehearsal.LIVE_DATA_ROOT
    catalog_path = data_root / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    versions = catalog["datasets"]["limit_up_pool"]["dates"]
    sample = next(item for entries in versions.values() for item in entries)
    manifest_path = Path(sample["manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows_path = Path(manifest["rows_uri"])
    large_checkpoint = next((data_root / ".checkpoints").glob("plate_members-*.json"))

    assert rehearsal.data_file_action("catalog.json", catalog_path.stat().st_size) == "copy"
    assert rehearsal.data_file_action("daily_status.json", (data_root / "daily_status.json").stat().st_size) == "copy"
    assert rehearsal.data_file_action(
        str((manifest_path.parent / "manifest.json").relative_to(data_root)), manifest_path.stat().st_size
    ) == "copy"
    assert rehearsal.data_file_action(str(rows_path.relative_to(data_root)), rows_path.stat().st_size) == "link"
    assert rehearsal.data_file_action(
        str(large_checkpoint.relative_to(data_root)), large_checkpoint.stat().st_size
    ) == "skip"
    assert rehearsal.data_file_action("guardian.log", (data_root / "guardian.log").stat().st_size) == "skip"


def test_refresh_output_uses_real_schedule_and_only_hides_staged_target_date() -> None:
    schedule = real_schedule()
    data_root = rehearsal.LIVE_DATA_ROOT
    trade_dates = json.loads((data_root / "trade_dates.json").read_text(encoding="utf-8"))
    catalog = json.loads((data_root / "catalog.json").read_text(encoding="utf-8"))
    target_date = max(catalog["datasets"]["limit_up_pool"]["dates"])
    event_date = min(day for day in trade_dates if day > target_date)

    dataset, resolved_date = rehearsal.event_output_partition(
        schedule, "limit_pool_d1", event_date, trade_dates,
    )
    staged_catalog, hidden_versions = rehearsal.hide_catalog_partition(catalog, dataset, resolved_date)

    assert dataset == "limit_up_pool"
    assert resolved_date == target_date
    assert hidden_versions == len(catalog["datasets"][dataset]["dates"][target_date])
    assert target_date not in staged_catalog["datasets"][dataset]["dates"]
    assert target_date in catalog["datasets"][dataset]["dates"]
