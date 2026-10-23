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


def test_report_requests_email_with_mail_enabled_without_audit_marker(tmp_path, monkeypatch):
    from datetime import datetime
    from examples import core_guardian
    from engine_core import daily_pipeline

    monkeypatch.setenv("CORE_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("CORE_ALLOW_FILE_FALLBACK", "0")
    (tmp_path / ".mail_enabled").touch()
    monkeypatch.setattr("engine_core.daily_pipeline._connect_redis", lambda: None)
    sends = []
    commands = []
    monkeypatch.setattr("engine_core.report_mailer.send_message",
                        lambda *args, **kwargs: sends.append(kwargs) or
                        SimpleNamespace(status="test_suppressed"))
    def run_report(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=daily_pipeline.main(command[2:]),
                               stdout="", stderr="")
    monkeypatch.setattr(core_guardian.subprocess, "run", run_report)
    opts = SimpleNamespace(write=False, td_write=False, simulate_clock=False,
                           email_self=True, run=False, job="")
    event = SimpleNamespace(event_id="auction_report_0926", data_date_offset=0,
                            timeout_seconds=60, start="09:26:00", args={"asof": "09:26:00"})
    result = core_guardian._command_handler("auction_report", opts, tmp_path)(
        event, datetime.fromisoformat("2026-10-21T09:26:30+08:00"), {}, False)
    reports = list((tmp_path / "reports" / "2026-10-21").glob("auction_0926/*"))
    assert reports and any(path.name == "manifest.json" for path in reports)
    assert "email_withheld" not in result
    assert any(["--email-send", "--email-to", "412826313@qq.com"] == command[i:i + 3]
               for command in commands for i in range(len(command) - 2))
    assert sends == [{"dry_run": False, "allow_recipients": ["412826313@qq.com"]}]


def test_close_report_handler_result_is_successful_contract(tmp_path, monkeypatch):
    from examples import core_guardian, guardian_handler
    from engine_core.guardian.scheduler import default_events

    event = next(item for item in default_events() if item.handler == "close_report")
    monkeypatch.setenv("CORE_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("GUARDIAN_DRY_RUN", "0")
    monkeypatch.setenv("GUARDIAN_EMAIL_SELF", "1")
    monkeypatch.setattr(core_guardian, "_command_handler", lambda *_:
                        lambda *_args: "report_generated")
    code, payload = guardian_handler._run({"event": event.event_id, "trade_date": "2026-10-20"})
    assert code == 0
    assert payload["status"] == "ok"
    assert payload["detail"] == "report_generated"
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
        # boot_apply_plan uses the direct reconciliation/fetch_item bridge and
        # is exercised by the configured runtime-reconciliation Go test.
        if event.get("trigger_type") == "startup" or event.get("handler") == "boot_apply_plan":
            continue
        _, payload = guardian_handler._run(_core_request(event["event_id"]))
        if event["handler"] == "close_capture":
            assert payload["status"] == "not_applicable"
        else:
            assert payload["status"] == "ok", (event["event_id"], payload)
    assert set(called) == {e["handler"] for e in events
                           if e.get("trigger_type") != "startup" and e.get("handler") != "boot_apply_plan"} - {"close_capture"}
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


def test_runtime_reconciliation_uses_enqueue_only_bridge(monkeypatch, tmp_path):
    from examples import guardian_handler
    from engine_core.guardian import startup

    called = {}
    monkeypatch.setenv("CORE_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(startup, "boot_reconcile", lambda *args, **kwargs: {
        "status": "ok", "detail": {"plan": [{"id": "light", "partition": "2026-10-22",
                                                    "action": "FETCH_NOW"}]}})

    def apply(*args, **kwargs):
        called.update(kwargs)
        return {"status": "ok", "detail": {"results": [], "deferred": []}}

    monkeypatch.setattr(startup, "boot_apply_plan", apply)
    code, payload = guardian_handler._run({
        "event": "runtime_reconcile_0900", "handler": "boot_apply_plan",
        "trade_date": "2026-10-22", "phase": "PRE_MARKET", "now": "2026-10-22T09:00:00+08:00"})

    assert code == 0 and payload["status"] == "ok"
    assert called["enqueue_only"] is True


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
    event = next(e for e in _schedule_events() if e.get("trigger_type") == "window")
    guardian_handler._run(_core_request(event["event_id"]))
    assert seen and not seen[0].write and not seen[0].email_self and not seen[0].run


def test_new_event_id_dispatches_from_configured_handler_without_python_event_entry(monkeypatch):
    from examples import guardian_handler, core_guardian
    from engine_core.guardian import commands

    seen = []
    monkeypatch.setattr(core_guardian, "_command_handler",
                        lambda name, *_: (seen.append(name), lambda *_args: "dispatched")[1])
    monkeypatch.setattr(commands, "validate_handler_postcondition", lambda *_: (True, ""))
    request = _core_request("config_only_extension")
    request.update(handler="kaipan_fetch", asof="2026-10-19T08:03:00+08:00", args={"job": "watch"})
    code, payload = guardian_handler._run(request)
    assert code == 0 and payload["status"] == "ok"
    assert seen == ["kaipan_fetch"]


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


def test_fakeclock_kaipan_job_persists_only_to_artifact_root():
    from engine_core.guardian.commands import build_handler_command
    from types import SimpleNamespace

    opts = SimpleNamespace(write=False, td_write=False, simulate_clock=True,
                           email_self=False, job="limit_up_pool")
    command = build_handler_command("kaipan_job", SimpleNamespace(), opts,
                                    ROOT / "artifacts", "2026-10-21", sys.executable, ROOT)
    assert command[-1] == "--execute"
    assert "--write" not in command


def test_auction_report_asof_is_clamped_to_0926_when_started_later(monkeypatch, tmp_path):
    from examples import core_guardian, guardian_handler

    seen = {}
    monkeypatch.setattr(core_guardian, "_command_handler", lambda name, opts, root: lambda event, now, ctx, degrade:
                        seen.update(start=event.start, now=now.isoformat()) or "ok")
    monkeypatch.setenv("CORE_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("GUARDIAN_DRY_RUN", "1")
    code, payload = guardian_handler._run(_core_request("auction_report_0926", "2026-10-22"))
    assert code == 0 and payload["status"] == "ok"
    assert seen["start"] == "09:26:00" and seen["now"].startswith("2026-10-22T09:26:00")


def test_premarket_handler_defers_when_another_same_date_owner_is_running():
    from examples import guardian_handler

    request = _core_request("startup_0900", "2026-10-22")
    request.update(handler="premarket_data", active_events={"startup_0830": "2026-10-22"})
    code, payload = guardian_handler._run(request)
    assert code == 0 and payload["status"] == "ok"
    assert payload["detail"] == "same_date_premarket_owner_still_running"
    request["event"] = "startup_0830"
    assert guardian_handler._premarket_owner_is_open(request) is False
