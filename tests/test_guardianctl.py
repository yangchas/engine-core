import json
from datetime import datetime, timedelta

from engine_core.guardian.management import schedule_payload
from engine_core.guardian.management import validate_schedule
from engine_core.guardian.scheduler import Guardian, TZ, _atomic_json

from examples.core_guardianctl import main


def setup_root(root, monkeypatch):
    guardian = root / "guardian"
    guardian.mkdir(parents=True)
    _atomic_json(guardian / "schedule.json", schedule_payload())
    now = datetime.now(TZ).isoformat()
    _atomic_json(guardian / "heartbeat.json", {"last_tick_at": now, "health": "READY", "phase": "AUCTION"})
    day = datetime.now(TZ).date().isoformat()
    _atomic_json(guardian / day / "state.json", {"trade_date": day, "events": {}})
    monkeypatch.setenv("CORE_DATA_ROOT", str(root))
    return guardian, day


def test_status_and_list_are_read_only(tmp_path, monkeypatch, capsys):
    guardian, day = setup_root(tmp_path, monkeypatch)
    before = set(path.name for path in guardian.iterdir())
    assert main(["status"]) == 0
    assert '"phase": "AUCTION"' in capsys.readouterr().out
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert '"event_id": "kaipan_refresh_0850"' in out
    assert set(path.name for path in guardian.iterdir()) == before


def test_manual_action_appends_request_only(tmp_path, monkeypatch, capsys):
    guardian, _ = setup_root(tmp_path, monkeypatch)
    payload = json.loads((guardian / "schedule.json").read_text())
    now = datetime.now(TZ)
    event = next(row for row in payload["events"] if row["event_id"] == "auction_0925_check")
    event["at"] = (now - timedelta(minutes=1)).strftime("%H:%M:%S")
    event["deadline"] = (now + timedelta(minutes=1)).strftime("%H:%M:%S")
    _atomic_json(guardian / "schedule.json", payload)
    assert main(["trigger", "auction_0925_check", "--date", datetime.now(TZ).date().isoformat(),
                 "--reason", "local operator check"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["result"] == "REQUESTED"
    rows = [json.loads(line) for line in (guardian / "control.jsonl").read_text().splitlines()]
    assert rows[0]["type"] == "REQUEST"
    assert rows[0]["verb"] == "trigger"


def test_unknown_event_and_missing_reason_are_contract_errors(tmp_path, monkeypatch, capsys):
    setup_root(tmp_path, monkeypatch)
    assert main(["trigger", "missing", "--date", datetime.now(TZ).date().isoformat(), "--reason", "test"]) == 2
    capsys.readouterr()
    assert main(["pause"]) == 2


def test_schedule_rejects_bad_values_cycles_and_alias_ids():
    def legacy_schedule_payload(revision=1):
        source = schedule_payload()
        event = {
            "enabled": True, "calendar": "every_day", "trigger_type": "window",
            "start": "08:00:00", "deadline": "08:15:00", "order": 1,
            "after": [], "gate": "always", "handler": "calendar_refresh",
            "timeout_seconds": 300, "max_attempts": 2, "retry_backoff": None,
            "retry_until_late": False, "silent_until_last": False,
            "catchup_mode": "skip", "on_deadline": "missed", "late_until": None,
            "cost": None, "phases": None, "on_phase_mismatch": None,
            "on_start": None, "data_date_offset": 0, "available_after": None,
            "depends_on": [], "args": None,
        }
        return {
            "schema_version": 2, "revision": revision, "timezone": "Asia/Shanghai",
            "global": source["global"],
            "events": [{**event, "event_id": "legacy_one"},
                       {**event, "event_id": "legacy_two", "start": "08:01:00"}],
        }

    def assert_rejected(payload, expected):
        try:
            validate_schedule(payload)
        except ValueError as exc:
            assert str(exc) == expected
        else:
            raise AssertionError(f"schedule accepted: {expected}")

    payload = legacy_schedule_payload()
    payload["events"][0]["timeout_seconds"] = 0
    assert_rejected(payload, "schedule_event_invalid")

    for old_field in ("catchup", "degraded_at_deadline", "retry_base_seconds",
                      "retry_max_seconds", "phase"):
        payload = legacy_schedule_payload()
        payload["events"][0][old_field] = None
        assert_rejected(payload, f"schedule_event_field_unknown:{old_field}")

    for field, value, error in (
        ("catchup_mode", "always", "schedule_catchup_mode_invalid"),
        ("on_deadline", "retry", "schedule_on_deadline_invalid"),
        ("retry_backoff", "0s", "schedule_event_invalid"),
        ("phases", ["PREMARKET"], "schedule_phases_invalid"),
    ):
        payload = legacy_schedule_payload()
        payload["events"][0][field] = value
        assert_rejected(payload, error)

    payload = legacy_schedule_payload()
    payload["events"][0]["depends_on"] = [payload["events"][1]["event_id"]]
    payload["events"][1]["depends_on"] = [payload["events"][0]["event_id"]]
    assert_rejected(payload, "schedule_dependency_cycle")
    old = legacy_schedule_payload()
    payload = legacy_schedule_payload(revision=2)
    alias = dict(payload["events"][0])
    alias["event_id"] = "renamed_alias"
    payload["events"].append(alias)
    try:
        validate_schedule(payload, old)
    except ValueError as exc:
        assert str(exc) == "schedule_alias_id_rejected"
    else:
        raise AssertionError("event alias accepted")


def test_schema_v3_schedule_profiles_ladder_at_list_and_target_contract(tmp_path):
    phases = schedule_payload()["global"]["phases"]
    payload = {
        "schema_version": 3,
        "revision": 1,
        "timezone": "Asia/Shanghai",
        "global": {"phases": phases},
        "profiles": {
            "backfill": {
                "catchup": "run_in_window",
                "ladder": [{
                    "name": "primary", "attempts": 2, "backoff": "1s*2",
                    "backoff_cap": "5m", "timeout": "60s", "budget_enabled": False,
                }],
            },
        },
        "events": [{
            "id": "daily_kline_backfill", "enabled": False, "calendar": "every_day",
            "trigger_type": "window", "at": ["01:00", "08:00"], "deadline": "08:15",
            "handler": "selftest", "profile": "backfill", "gate": "always",
            "args": {"target": "prev_trade_day"},
        }],
    }

    events = validate_schedule(payload)
    assert len(events) == 1
    assert events[0].event_id == "daily_kline_backfill"
    assert events[0].start == "01:00:00"
    assert events[0].deadline == "08:15:00"
    assert events[0].enabled is False

    invalid = json.loads(json.dumps(payload))
    invalid["events"][0]["profile"] = "missing"
    try:
        validate_schedule(invalid)
    except ValueError as exc:
        assert str(exc) == "schedule_profile_unknown:missing"
    else:
        raise AssertionError("unknown profile accepted")

    invalid = json.loads(json.dumps(payload))
    invalid["profiles"]["backfill"]["ladder"] = []
    try:
        validate_schedule(invalid)
    except ValueError as exc:
        assert str(exc) == "schedule_ladder_empty:profile:backfill"
    else:
        raise AssertionError("empty profile ladder accepted")

    invalid = json.loads(json.dumps(payload))
    del invalid["profiles"]["backfill"]["ladder"][0]["timeout"]
    try:
        validate_schedule(invalid)
    except ValueError as exc:
        assert str(exc) == "schedule_ladder_timeout_required:profile:backfill:0"
    else:
        raise AssertionError("ladder rung without timeout accepted")

    invalid = json.loads(json.dumps(payload))
    invalid["events"][0]["at"] = ["not-a-time"]
    try:
        validate_schedule(invalid)
    except ValueError as exc:
        assert str(exc) == "schedule_at_invalid:daily_kline_backfill:0"
    else:
        raise AssertionError("invalid at-list entry accepted")

    invalid = json.loads(json.dumps(payload))
    invalid["events"][0]["unknown"] = True
    try:
        validate_schedule(invalid)
    except ValueError as exc:
        assert str(exc) == "schedule_v3_field_unknown:events[0]:unknown"
    else:
        raise AssertionError("unknown v3 event field accepted")

    path = tmp_path / "schedule.json"
    state_root = tmp_path / "state"
    applied = json.loads(json.dumps(payload))
    applied["revision"] = 2
    path.write_text(json.dumps(applied))
    guardian = Guardian(validate_schedule(payload), calendar_dates={"2026-10-26"},
                        state_root=state_root, schedule_path=path, config_revision=1,
                        config_payload=payload)
    actions = []
    guardian._reload_schedule(actions)
    assert actions == [{"status": "CONFIG_APPLIED", "revision": 2}]
    assert guardian.config_revision == 2

    rejected = json.loads(json.dumps(applied))
    rejected["revision"] = 3
    rejected["events"][0]["profile"] = "missing"
    path.write_text(json.dumps(rejected))
    actions = []
    guardian._reload_schedule(actions)
    assert actions[0]["status"] == "CONFIG_REJECTED"
    records = [json.loads(line) for line in (state_root / "control.jsonl").read_text().splitlines()]
    assert [record["type"] for record in records] == ["CONFIG_APPLIED", "CONFIG_REJECTED"]
