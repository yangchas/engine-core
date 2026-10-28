import json
import hashlib
import re
from dataclasses import fields
from pathlib import Path

from engine_core.guardian.management import GO_EVENT_FIELDS, GO_V3_EVENT_FIELDS, load_schedule
from engine_core.guardian.scheduler import EventSpec, default_events


ROOT = Path(__file__).resolve().parents[1]


def test_schedule_fields_match_go_migrator_and_native_fixture_provenance():
    source_path = ROOT / "src/engine_core/guardian/schedule.json"
    native_path = ROOT / "native/guardiand/testdata/schedule.json"
    provenance_path = ROOT / "native/guardiand/testdata/provenance.json"
    go_source = (ROOT / "native/guardiand/main.go").read_text(encoding="utf-8")
    v2_event_struct = re.search(
        r'type scheduleV2Event struct\s*\{(?P<body>.*?)\n\}',
        go_source,
        re.DOTALL,
    )
    assert v2_event_struct, "scheduleV2Event struct not found"
    v2_fields = set(re.findall(r'`json:"([^",]+)"`', v2_event_struct.group("body")))
    assert v2_fields == GO_EVENT_FIELDS
    source_bytes = source_path.read_bytes()
    schedule = json.loads(source_bytes)
    if schedule["schema_version"] == 3:
        event_struct = re.search(
            r'type scheduleV3Event struct\s*\{(?P<body>.*?)\n\}',
            go_source,
            re.DOTALL,
        )
        assert event_struct, "scheduleV3Event struct not found"
        go_fields = set(re.findall(r'`json:"([^",]+)"`', event_struct.group("body")))
        assert go_fields == GO_V3_EVENT_FIELDS
    else:
        go_fields = v2_fields
    assert all(set(event) <= go_fields for event in schedule["events"])
    assert native_path.read_bytes() == source_bytes
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    assert provenance["sha256"] == hashlib.sha256(source_bytes).hexdigest()
    events = {event["event_id"]: event for event in schedule["events"]}
    baostock = [event for event in schedule["events"] if event.get("handler") == "baostock_probe"]
    assert len(baostock) == 4
    assert all(event["late_until"] == "23:30:00" and
               event["late_until_date_offset_days"] == 1 and
               event["late_until_date_offset_error_codes"] == ["BAOSTOCK_LOCAL_GAP_WAIT"] and
               event["late_until_clock_by_error_code"] == {"BAOSTOCK_LOCAL_GAP_WAIT": "08:00:00"} and
               "BAOSTOCK_LOCAL_GAP_WAIT" in event["args"]["failure_classes"]["retryable"]["error_codes"] and
               event["args"]["baostock_gapfill_policy"]["minimum_coverage"] == 0.90
               for event in baostock)
    assert schedule["profiles"]["policy_11"]["ladder"][0]["attempts"] == 32
    assert schedule["profiles"]["policy_11"]["ladder"][0]["backoff"] == "30m*1"
    assert events["daily_kline_backfill"]["enabled"] is True
    assert events["daily_kline_backfill"]["handler"] == "daily_kline_backfill"
    assert events["daily_kline_backfill"]["at"] == ["01:00", "08:00"]


def test_default_events_load_all_window_and_startup_definitions_from_config(tmp_path):
    source = json.loads((ROOT / "src/engine_core/guardian/schedule.json").read_text())
    source["events"][0]["at"] = "00:07:00"
    source["events"].append({
        "event_id": "boot_test_extension", "enabled": True, "calendar": "every_day",
        "trigger_type": "startup", "order": 60, "after": ["fetch_item"],
        "gate": "always", "handler": "calendar_refresh", "timeout_seconds": 300,
    })
    path = tmp_path / "schedule.json"
    path.write_text(json.dumps(source))

    events, config = load_schedule(path)

    assert next(event for event in events if event.event_id == source["events"][0]["event_id"]).start == "00:07:00"
    extension = next(event for event in events if event.event_id == "boot_test_extension")
    assert extension.trigger_type == "startup"
    assert extension.phase == "NIGHT"
    assert extension.catchup is False
    assert extension.retry_base_seconds == 1
    assert extension.retry_max_seconds == 300
    baostock = next(event for event in events if event.event_id == "baostock_probe_1730")
    assert baostock.late_until_clock_by_error_code == {"BAOSTOCK_LOCAL_GAP_WAIT": "08:00:00"}
    derived = EventSpec("derived", "09:20:00", "09:21:00", "always", "calendar_refresh",
                        catchup_mode="run_in_window", on_deadline="degrade",
                        retry_backoff="1m", phases=("any",))
    assert derived.catchup is True
    assert derived.degraded_at_deadline is True
    assert derived.phase == "AUCTION"
    assert derived.retry_base_seconds == 60
    assert derived.retry_max_seconds == 300
    legacy_fields = {"catchup", "degraded_at_deadline", "retry_base_seconds",
                     "retry_max_seconds", "phase"}
    assert not legacy_fields.intersection(field.name for field in fields(EventSpec))
    try:
        derived.catchup = False
    except AttributeError:
        pass
    else:
        raise AssertionError("derived compatibility property is writable")
    assert config["global"]["heavy_max_load5"] == source["global"]["heavy_max_load5"]
    assert config["global"]["heavy_load_defer_max_polls"] == source["global"]["heavy_load_defer_max_polls"]


def test_registry_trigger_event_resolves_to_the_single_schedule_start():
    registry = json.loads((ROOT / "src/engine_core/guardian/datasets.registry.json").read_text())
    schedule = json.loads((ROOT / "src/engine_core/guardian/schedule.json").read_text())
    events = {event["event_id"]: event for event in schedule["events"]}
    for item in registry["items"]:
        policy = item.get("late_policy") or {}
        if policy.get("trigger_event"):
            assert policy["trigger_event"] in events
            assert policy.get("trigger_at") is None



def test_runtime_reconciliation_checkpoints_are_configured_and_use_fetch_plan():
    import json
    from pathlib import Path

    payload = json.loads((Path(__file__).resolve().parents[1] /
                          "src/engine_core/guardian/schedule.json").read_text())
    events = {event["event_id"]: event for event in payload["events"]}
    checkpoints = {
        "0900": ("09:00:00", "PRE_MARKET"),
        "0935": ("09:35:00", "OPEN"),
        "1000": ("10:00:00", "INTRADAY_AM"),
        "1145": ("11:45:00", "MIDDAY_BREAK"),
        "1330": ("13:30:00", "INTRADAY_PM"),
        "1520": ("15:20:00", "CLOSE"),
        "1540": ("15:40:00", "POST_MARKET"),
        "1745": ("17:45:00", "POST_MARKET"),
        "1930": ("19:30:00", "POST_MARKET"),
        "2100": ("21:00:00", "POST_MARKET"),
    }
    for suffix, (start, phase) in checkpoints.items():
        event = events[f"runtime_reconcile_{suffix}"]
        assert event["trigger_type"] == "window"
        assert event["at"] == start
        assert event["handler"] == "boot_apply_plan"
        assert event["cost"] == "light"
        assert event["phases"] is None
        assert event["args"]["reconciliation_checkpoint"] == phase
        assert event["args"]["checkpoint_reason"]
        profile = payload["profiles"][event["profile"]]
        assert profile["ladder"][0]["attempts"] == 1
        assert profile["catchup"] == "run_in_window"
