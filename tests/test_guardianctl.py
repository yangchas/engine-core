import json
from datetime import datetime, timedelta

from engine_core.guardian.management import schedule_payload
from engine_core.guardian.management import validate_schedule
from engine_core.guardian.scheduler import TZ, _atomic_json

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
    event["start"] = (now - timedelta(minutes=1)).strftime("%H:%M:%S")
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
    def assert_rejected(payload, expected):
        try:
            validate_schedule(payload)
        except ValueError as exc:
            assert str(exc) == expected
        else:
            raise AssertionError(f"schedule accepted: {expected}")

    payload = schedule_payload()
    payload["events"][0]["timeout_seconds"] = 0
    assert_rejected(payload, "schedule_event_invalid")

    for old_field in ("catchup", "degraded_at_deadline", "retry_base_seconds",
                      "retry_max_seconds", "phase"):
        payload = schedule_payload()
        payload["events"][0][old_field] = None
        assert_rejected(payload, f"schedule_event_field_unknown:{old_field}")

    for field, value, error in (
        ("catchup_mode", "always", "schedule_catchup_mode_invalid"),
        ("on_deadline", "retry", "schedule_on_deadline_invalid"),
        ("retry_backoff", "0s", "schedule_event_invalid"),
        ("phases", ["PREMARKET"], "schedule_phases_invalid"),
    ):
        payload = schedule_payload()
        payload["events"][0][field] = value
        assert_rejected(payload, error)

    payload = schedule_payload()
    payload["events"][0]["depends_on"] = [payload["events"][1]["event_id"]]
    payload["events"][1]["depends_on"] = [payload["events"][0]["event_id"]]
    assert_rejected(payload, "schedule_dependency_cycle")
    old = schedule_payload()
    payload = schedule_payload(revision=2)
    alias = dict(payload["events"][0])
    alias["event_id"] = "renamed_alias"
    payload["events"].append(alias)
    try:
        validate_schedule(payload, old)
    except ValueError as exc:
        assert str(exc) == "schedule_alias_id_rejected"
    else:
        raise AssertionError("event alias accepted")
