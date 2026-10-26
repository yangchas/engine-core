import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
HANDLER = ROOT / "examples" / "guardian_selftest_handler.py"


def invoke(tmp_path, command, request):
    env = os.environ.copy()
    env["SELFTEST_DIR"] = str(tmp_path)
    return subprocess.run(
        [sys.executable, str(HANDLER), command],
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=10,
    )


def test_selftest_handler_logs_calls_and_implements_local_protocol(tmp_path):
    request = {
        "contract": 1,
        "event": "probe_event",
        "instance": "probe_event@1",
        "attempt": 1,
        "trade_date": "2026-10-26",
        "data_date": "2026-10-25",
        "asof": "2026-10-26T09:30:00+08:00",
        "degrade_requested": False,
        "args": {"behavior": "fail:1", "token": "unchanged"},
    }
    failed = invoke(tmp_path, "run", request)
    assert failed.returncode == 75
    assert json.loads(failed.stdout)["error_code"] == "INTERNAL_ERROR"

    succeeded = invoke(tmp_path, "run", {**request, "attempt": 2})
    assert succeeded.returncode == 0
    assert json.loads(succeeded.stdout)["status"] == "ok"

    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert len(calls) == 2
    assert calls[0]["args"] == request["args"]
    assert calls[0]["trade_date"] == request["trade_date"]
    assert calls[0]["data_date"] == request["data_date"]
    assert datetime.fromisoformat(calls[0]["wall_clock"]).utcoffset() == datetime.now(ZoneInfo("Asia/Shanghai")).utcoffset()

    gate_request = {"gate": {"ref": "smoke"}}
    blocked = invoke(tmp_path, "gate", gate_request)
    assert blocked.returncode == 75
    assert json.loads(blocked.stdout)["status"] == "not_ready"
    gates = tmp_path / "gates"
    gates.mkdir(exist_ok=True)
    (gates / "smoke").write_text("ready\n")
    ready = invoke(tmp_path, "gate", gate_request)
    assert ready.returncode == 0
    assert json.loads(ready.stdout)["status"] == "ready"

    alert = invoke(tmp_path, "alert", {"event": "probe_event", "reason": "test"})
    assert alert.returncode == 0
    assert len((tmp_path / "alerts.jsonl").read_text().splitlines()) == 1
