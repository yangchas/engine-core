import pytest
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace



ROOT = Path(__file__).resolve().parents[1]


def _schedule_events():
    schedule = json.loads((ROOT / "src/engine_core/guardian/schedule.json").read_text())
    return schedule.get("events", schedule)


def _core_request(event_id, trade_date="2026-10-19"):
    # Shape written by guardiand runtime.go invoke(): "event" is the event id string.
    return {"contract": 1, "event": event_id, "instance": event_id, "attempt": 1,
            "run_id": "t-" + event_id, "idem_key": trade_date + "/" + event_id,
            "trade_date": trade_date, "data_date": trade_date, "degrade_requested": False,
            "sandbox": {"on": False, "root": ""}, "args": None,
            "gate": {"ref": "", "params": None, "probe": False}}


def test_handler_run_contract_round_trip_close_capture_not_applicable():
    event_id = next(e["event_id"] for e in _schedule_events() if e["handler"] == "close_capture")
    result = subprocess.run([sys.executable, "examples/guardian_handler.py", "run"],
                            cwd=ROOT, input=json.dumps(_core_request(event_id)), text=True,
                            capture_output=True)
    payload = json.loads(result.stdout.splitlines()[-1])
    assert result.returncode == 0
    assert payload["contract"] == 1 and payload["status"] == "not_applicable"
    assert payload["error_code"] == ""


def test_report_commands_wait_for_audit_marker_without_raising(tmp_path, monkeypatch):
    from datetime import date, datetime
    from examples import core_guardian

    import shutil
    from pathlib import Path
    real_calendar = Path.home() / "core-data" / "trade_calendar"
    if not real_calendar.is_dir():
        pytest.skip("real Core trade calendar not available")
    shutil.copytree(real_calendar, tmp_path / "trade_calendar")  # real calendar copy: previous(10-20) == 10-19

    calls = []
    # A failing handler raises an alert; tests must never reach real SMTP.
    monkeypatch.setattr("engine_core.report_mailer.load_smtp_config", lambda *a, **k: None)
    monkeypatch.setattr("engine_core.report_mailer.send_message",
                        lambda *a, **k: SimpleNamespace(status="test_suppressed"))
    monkeypatch.setattr("examples.core_guardian.subprocess.run", lambda *a, **k:
                        calls.append(a) or SimpleNamespace(returncode=0, stdout="report ok", stderr=""))
    opts = SimpleNamespace(write=True, td_write=False, simulate_clock=False,
                           email_self=True, run=True, job="")
    names = {"auction_report": "auction", "open_report": "open",
             "close_report": "close", "night_report": "night"}
    for name, stage in names.items():
        event = SimpleNamespace(data_date_offset=0, timeout_seconds=10, start="09:26:00")
        handler = core_guardian._command_handler(name, opts, tmp_path)
        waiting = handler(event, datetime.fromisoformat("2026-10-20T18:19:00+08:00"), {}, False)
        target_day = "2026-10-19" if name == "night_report" else "2026-10-20"
        assert waiting == "NOT_APPLICABLE:waiting_audit_ok:" + stage
        assert calls == []
        marker = tmp_path / "guardian" / target_day / "audit_ok" / stage
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        assert handler(event, datetime.fromisoformat("2026-10-20T18:19:00+08:00"), {}, False) == "report ok"
        assert len(calls) == 1
        calls.clear()


def test_missing_close_audit_marker_is_not_applicable_contract(tmp_path, monkeypatch):
    from examples import core_guardian, guardian_handler
    from engine_core.guardian.scheduler import default_events

    event = next(item for item in default_events() if item.handler == "close_report")
    monkeypatch.setenv("CORE_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("GUARDIAN_DRY_RUN", "0")
    monkeypatch.setenv("GUARDIAN_EMAIL_SELF", "1")
    monkeypatch.setattr(core_guardian, "_command_handler", lambda *_:
                        lambda *_args: "NOT_APPLICABLE:waiting_audit_ok:close")
    code, payload = guardian_handler._run({"event": event.event_id, "trade_date": "2026-10-20"})
    assert code == 0
    assert payload["status"] == "not_applicable"
    assert payload["detail"] == "waiting_audit_ok:close"
    assert payload["retryable"] is False


def test_unknown_event_id_is_config_error():
    result = subprocess.run([sys.executable, "examples/guardian_handler.py", "run"],
                            cwd=ROOT, input=json.dumps(_core_request("no_such_event")), text=True,
                            capture_output=True)
    assert result.returncode == 64
    assert json.loads(result.stdout.splitlines()[-1])["error_code"] == "CONFIG_ERROR"


def test_core_startup_missed_summary_is_one_dry_run_mail():
    # Shape written by guardiand applyStartup(): one summary listing all missed instances.
    ids = [e["event_id"] for e in _schedule_events()][:42]
    request = {"contract": 1, "event": "alert", "reason": "MISSED",
               "request": {"count": len(ids), "instances": ids, "summary": True}}
    result = subprocess.run([sys.executable, "examples/guardian_handler.py", "alert", "--dry-run"],
                            cwd=ROOT, input=json.dumps(request), text=True, capture_output=True)
    payload = json.loads(result.stdout.splitlines()[-1])
    assert result.returncode == 0 and payload["status"] == "dry_run"
    assert payload["events"] == 42
    assert all(f"{i}: MISSED (1)" in payload["body"] for i in ids)


def test_core_single_event_failure_alert_is_formatted():
    request = {"contract": 1, "event": "alert", "reason": "CIRCUIT_OPEN",
               "request": {"event": "startup_0830", "date": "2026-10-20", "error": "timeout"}}
    result = subprocess.run([sys.executable, "examples/guardian_handler.py", "alert", "--dry-run"],
                            cwd=ROOT, input=json.dumps(request), text=True, capture_output=True)
    payload = json.loads(result.stdout.splitlines()[-1])
    assert "startup_0830: CIRCUIT_OPEN (1)" in payload["body"]


def test_every_schedule_event_dispatches_to_its_handler(monkeypatch):
    from examples import guardian_handler
    from examples import core_guardian

    events = _schedule_events()
    called = []
    seen_opts = []

    def fake_factory(name, opts, root):
        called.append(name)
        seen_opts.append(opts)
        return lambda *_: "dispatched"
    monkeypatch.setattr(core_guardian, "_command_handler", fake_factory)
    from engine_core.guardian import commands
    monkeypatch.setattr(commands, "validate_handler_postcondition", lambda *_: (True, ""))
    for event in events:
        _, payload = guardian_handler._run(_core_request(event["event_id"]))
        if event["handler"] == "close_capture":
            assert payload["status"] == "not_applicable"
        else:
            assert payload["status"] == "ok", (event["event_id"], payload)
    assert set(called) == {e["handler"] for e in events} - {"close_capture"}
    assert all(o.write and o.run for o in seen_opts)  # live mode writes, like the old guardian


def test_successful_command_fails_when_postcondition_is_missing(monkeypatch):
    from examples import guardian_handler
    from examples import core_guardian
    from engine_core.guardian import commands

    event = next(e for e in _schedule_events() if e["handler"] == "premarket_data")
    monkeypatch.setattr(core_guardian, "_command_handler",
                        lambda *_: (lambda *_args: "command_succeeded"))
    monkeypatch.setattr(commands, "validate_handler_postcondition",
                        lambda *_: (False, "symbol_universe_manifest_missing"))
    _, payload = guardian_handler._run(_core_request(event["event_id"]))
    assert payload["status"] == "failed"
    assert payload["error_code"] == "POSTCONDITION_FAILED"
    assert payload["retryable"] is True


def test_dry_run_skips_postcondition_validation(monkeypatch):
    from examples import guardian_handler
    from examples import core_guardian
    from engine_core.guardian import commands

    event = next(e for e in _schedule_events() if e["handler"] == "premarket_data")
    monkeypatch.setattr(core_guardian, "_command_handler",
                        lambda *_: (lambda *_args: "command_succeeded"))
    monkeypatch.setattr(commands, "validate_handler_postcondition",
                        lambda *_: (_ for _ in ()).throw(AssertionError("validation called")))
    monkeypatch.setenv("GUARDIAN_DRY_RUN", "1")
    _, payload = guardian_handler._run(_core_request(event["event_id"]))
    assert payload["status"] == "ok"


def test_dry_run_env_disables_writes_and_mail(monkeypatch):
    from examples import guardian_handler
    from examples import core_guardian

    seen = []
    monkeypatch.setattr(core_guardian, "_command_handler",
                        lambda name, opts, root: (seen.append(opts), (lambda *_: "x"))[1])
    monkeypatch.setenv("GUARDIAN_DRY_RUN", "1")
    guardian_handler._run(_core_request(_schedule_events()[0]["event_id"]))
    assert seen and not seen[0].write and not seen[0].email_self and not seen[0].run


def test_gate_names_from_stdin_and_cli_are_recognized_and_unknown_is_not_ready():
    events = _schedule_events()
    for event in events:
        gate = event.get("gate")
        if not gate:
            continue
        req = _core_request(event["event_id"]); req["gate"] = {"ref": gate, "probe": True}
        result = subprocess.run([sys.executable, "examples/guardian_handler.py", "gate"],
                                cwd=ROOT, input=json.dumps(req), text=True, capture_output=True)
        payload = json.loads(result.stdout.splitlines()[-1])
        assert payload["status"] in {"ready", "not_ready"}
        assert payload["reason"] != "unknown_gate", gate
    result = subprocess.run([sys.executable, "examples/guardian_handler.py", "gate", "unknown"],
                            cwd=ROOT, text=True, capture_output=True, input="{}")
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "status": "not_ready", "reason": "unknown_gate"}


def test_shared_command_builder_preserves_legacy_command_shape():
    from engine_core.guardian.commands import build_handler_command
    from types import SimpleNamespace

    event = SimpleNamespace(data_date_offset=0, start="09:26:00", timeout_seconds=30)
    opts = SimpleNamespace(write=False, td_write=False, simulate_clock=False, email_self=False)
    command = build_handler_command("auction_report", event, opts, ROOT / "artifacts",
                                    "2026-10-19", sys.executable, ROOT)
    assert command == [sys.executable, str(ROOT / "examples/run_daily_reports.py"),
                       "--stage", "auction", "--trade-date", "2026-10-19",
                       "--out-root", str(ROOT / "artifacts/reports"), "--asof", "09:26:00",
                       "--dry-run"]
