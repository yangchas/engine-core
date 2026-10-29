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


def _duration_seconds(value):
    match = re.fullmatch(r"(\d+)([sm])", value)
    assert match, f"unsupported duration {value!r}"
    scale = {"s": 1, "m": 60}[match.group(2)]
    return int(match.group(1)) * scale


def _assert_registry_schedule_text_consistency(registry, schedule):
    events = {event["event_id"]: event for event in schedule["events"]}
    handlers = {event.get("handler") for event in schedule["events"]}
    profiles = schedule["profiles"]
    event_tag = re.compile(r"\bevent=([a-z0-9_]+)(?:\s+profile=([a-z0-9_]+))?")
    rung_claim = re.compile(
        r"\b([a-z][a-z0-9_]*)\s+attempts=(\d+)\s+backoff=(\d+[sm]\*\d+)"
        r"(?:\s+timeout=(\d+[sm]))?(?:\s+cap=(\d+[sm]))?"
        r"(?:\s+until=(\d{2}:\d{2}))?(?:\s+retry_until=(\d{2}:\d{2}))?"
    )

    for item in registry["items"]:
        owner = item.get("guardian_event")
        assert owner is None or owner in events or owner in handlers, (
            f"{item['id']}.guardian_event does not resolve in schedule: {owner}"
        )
        for text in [str(item.get("retry", "")), *map(str, item.get("degrade", []))]:
            tags = list(event_tag.finditer(text))
            has_schedule_claim = re.search(
                r"\b(?:max_attempts|retry_backoff|attempts|backoff)=|\brungs=|"
                r"\bschedule\s+(?:max_attempts|profile|policy)\b",
                text,
            )
            assert not has_schedule_claim or tags, (
                f"{item['id']} schedule retry/degrade claim lacks an event= reference: {text}"
            )
            for index, tag in enumerate(tags):
                event_id, declared_profile = tag.groups()
                assert event_id in events, f"{item['id']} references missing schedule event {event_id}"
                event = events[event_id]
                profile_name = declared_profile or event.get("profile")
                if declared_profile:
                    assert declared_profile in profiles, (
                        f"{item['id']} references missing profile {declared_profile}"
                    )
                    assert event.get("profile") == declared_profile, (
                        f"{item['id']} profile mismatch for {event_id}: {declared_profile}"
                    )
                end = tags[index + 1].start() if index + 1 < len(tags) else len(text)
                segment = text[tag.end():end]

                rung_names = None
                rung_match = re.search(r"\brungs=([a-z0-9_]+(?:>[a-z0-9_]+)*)", segment)
                if rung_match:
                    rung_names = rung_match.group(1).split(">")
                    assert profile_name in profiles, f"{item['id']} has no profile for rungs"
                    configured = [rung["name"] for rung in profiles[profile_name]["ladder"]]
                    assert rung_names == configured, (
                        f"{item['id']} rung order {rung_names} != {configured}"
                    )

                claims = list(rung_claim.finditer(segment))
                for claim in claims:
                    assert profile_name in profiles, f"{item['id']} has no profile for ladder claim"
                    name, attempts, backoff, timeout, cap, until, retry_until = claim.groups()
                    ladder = {rung["name"]: rung for rung in profiles[profile_name]["ladder"]}
                    assert name in ladder, f"{item['id']} references missing rung {name} in {profile_name}"
                    rung = ladder[name]
                    assert rung["attempts"] == int(attempts), (
                        f"{item['id']} {name} attempts={attempts} != schedule {rung['attempts']}"
                    )
                    assert rung["backoff"] == backoff, (
                        f"{item['id']} {name} backoff={backoff} != schedule {rung['backoff']}"
                    )
                    if timeout:
                        assert _duration_seconds(rung["timeout"]) == _duration_seconds(timeout)
                    if cap:
                        assert rung.get("backoff_cap") == cap
                    if until:
                        assert event.get("late_until", "")[:5] == until
                    if retry_until:
                        assert rung.get("args", {}).get("retry_until") == f"{retry_until}:00"

                direct_attempts = re.search(r"\bmax_attempts=(\d+)\b", segment)
                if direct_attempts:
                    actual = event.get("max_attempts")
                    if actual is None and profile_name:
                        ladder = profiles[profile_name]["ladder"]
                        if len(ladder) == 1:
                            actual = ladder[0]["attempts"]
                    assert actual == int(direct_attempts.group(1)), (
                        f"{item['id']} {event_id} max_attempts={direct_attempts.group(1)} != schedule {actual}"
                    )
                direct_backoff = re.search(r"\bretry_backoff=(\d+[sm])\b", segment)
                if direct_backoff:
                    actual = event.get("retry_backoff")
                    if actual is None and profile_name:
                        actual = profiles[profile_name].get("retry_backoff")
                    assert actual == direct_backoff.group(1), (
                        f"{item['id']} {event_id} retry_backoff={direct_backoff.group(1)} != schedule {actual}"
                    )
                direct_timeout = re.search(r"\btimeout=(\d+[sm])\b", segment)
                if direct_timeout and not claims:
                    actual = event.get("timeout_seconds")
                    if actual is None and profile_name:
                        ladder = profiles[profile_name]["ladder"]
                        if len(ladder) == 1:
                            actual = _duration_seconds(ladder[0]["timeout"])
                    assert actual is not None and actual == _duration_seconds(direct_timeout.group(1)), (
                        f"{item['id']} {event_id} timeout={direct_timeout.group(1)} != schedule {actual}"
                    )


def test_registry_retry_and_degrade_claims_match_schedule_events_and_profiles():
    registry = json.loads((ROOT / "src/engine_core/guardian/datasets.registry.json").read_text())
    schedule = json.loads((ROOT / "src/engine_core/guardian/schedule.json").read_text())
    items = {item["id"]: item for item in registry["items"]}
    events = {event["event_id"]: event for event in schedule["events"]}

    daily_text = " ".join([items["daily_kline"]["retry"], *items["daily_kline"]["degrade"]])
    assert "event=daily_kline_close_1505 profile=daily_kline_close" in daily_text
    assert [rung["name"] for rung in schedule["profiles"]["daily_kline_close"]["ladder"]] == [
        "wencai", "q2_final"
    ]
    assert events["daily_kline_close_1505"]["profile"] == "daily_kline_close"

    _assert_registry_schedule_text_consistency(registry, schedule)

    redis_events = [
        event for event in schedule["events"]
        if event.get("args", {}).get("dataset") == "limit_up_pool_today"
        and event.get("args", {}).get("tier") == "redis"
    ]
    assert redis_events
    assert all(event.get("max_attempts") == 3 for event in redis_events)
    assert all(event.get("retry_backoff") == "1s" for event in redis_events)
    close_confirm = events["limit_up_today_close_confirm_152000"]
    assert close_confirm["max_attempts"] == 1
    assert close_confirm["timeout_seconds"] == 300

    today = items["limit_up_pool_today"]
    checkpoint = items["intraday_tick_stream"]
    assert "td_sink" not in today
    assert today["artifact"]["catalog_dataset"] is None
    assert checkpoint["artifact"]["catalog_dataset"] is None
    assert "Redis" in checkpoint["rule"]
    assert "不落地到 TD" in checkpoint["rule"]
    assert "no TD persistence" in checkpoint["notes"]
    assert "no catalog temporary partition" in checkpoint["notes"]


def test_hot_plate_capture_schedule_matches_contract_windows_and_retry_budgets():
    schedule = json.loads((ROOT / "src/engine_core/guardian/schedule.json").read_text())
    events = [event for event in schedule["events"]
              if event.get("handler") == "kaipan_hot_capture" and
              event.get("event_id", "").startswith("kaipan_hot_")]
    expected = {
        "09:16:10": "09:17:30", "09:21:10": "09:23:00",
        "09:25:20": "09:25:50", "09:28:30": "09:30:00",
        "09:32:20": "09:33:30", "09:35:20": "09:36:30",
        "09:40:20": "09:41:30", "09:45:20": "09:46:30",
        "10:10:00": "10:12:00", "10:20:00": "10:22:00",
        "10:30:00": "10:32:00", "10:45:00": "10:47:00",
        "11:00:00": "11:02:00", "11:25:00": "11:27:00",
        "13:30:00": "13:32:00", "14:30:00": "14:32:00",
        "14:50:00": "14:52:00", "15:10:00": "15:12:00",
    }
    assert len(events) == 18
    assert {event["at"]: event["deadline"] for event in events} == expected
    profiles = schedule["profiles"]
    for event in events:
        assert event["args"]["max_pages"] == 5
        assert event["args"]["datasets"] == ["hot_plates", event["event_id"]]
        ladder = profiles[event["profile"]]["ladder"][0]
        assert ladder["attempts"] >= 2
        if event["at"] == "09:25:20":
            assert ladder["attempts"] == 2
            assert event["deadline"] == "09:25:50"
            assert ladder["timeout"] == "10s"
            assert ladder["backoff"] == "2s*1"
    reconcile = next(event for event in schedule["events"]
                     if event.get("event_id") == "hot_plates_gap_reconcile_151500")
    assert reconcile["at"] == "15:15:00"
    assert reconcile["args"] == {"reconcile_only": True, "datasets": ["hot_plates_gaps"]}
    assert schedule["profiles"][reconcile["profile"]]["ladder"][0]["attempts"] == 1


def test_hot_plate_history_has_a_premarket_gap_only_schedule():
    schedule = json.loads((ROOT / "src/engine_core/guardian/schedule.json").read_text())
    event = next(event for event in schedule["events"]
                 if event.get("event_id") == "hot_plates_history_0815")

    assert event["handler"] == "kaipan_hot_capture"
    assert event["at"] == "08:15:00"
    assert event["deadline"] == "08:30:00"
    assert event["on_deadline"] == "missed"
    assert event["args"]["hot_plates_history_days"] == 3
    assert event["args"]["datasets"] == ["hot_plates_history", "hot_plates_history_days=3"]
    profile = schedule["profiles"][event["profile"]]["ladder"][0]
    assert profile["attempts"] == 1
    assert profile["timeout"] == "300s"



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
