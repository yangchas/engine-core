import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GO_ROOT = Path("/home/exedev/repos/engine_core-guardian/native/guardiand")


def test_guardiand_python_bridge_end_to_end(tmp_path):
    go = shutil.which("go")
    if not go:
        pytest.skip("go toolchain unavailable")
    if not (GO_ROOT / "go.mod").is_file():
        pytest.skip("guardiand Go source unavailable")
    binary = tmp_path / "guardiand"
    built = subprocess.run([go, "build", "-o", str(binary), "."], cwd=GO_ROOT,
                           capture_output=True, text=True, timeout=120)
    assert built.returncode == 0, built.stderr

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    starts = [(now + timedelta(seconds=4 + i * 3)).strftime("%H:%M:%S") for i in range(4)]
    deadline = (now + timedelta(seconds=20)).strftime("%H:%M:%S")
    handler = [sys.executable, str(ROOT / "examples/guardian_handler.py"), "run"]
    gate = [sys.executable, "-c", "import sys; print('{\"status\":\"not_ready\",\"reason\":\"test\"}')"]
    alert = [sys.executable, str(ROOT / "examples/guardian_handler.py"), "alert", "--dry-run"]
    events = []
    for event_id, hname, gate_ref, start in [
        ("calendar_refresh_0800", "calendar_refresh", "", starts[0]),
        ("kaipan_hot_092100", "kaipan_hot_capture", "", starts[1]),
        ("close_mark_1505", "close_capture", "", starts[2]),
        ("intraday_100005", "intraday_data", "test_block", starts[3]),
    ]:
        events.append({"id": event_id, "enabled": True, "calendar": "every_day",
                       "trigger": {"type": "window", "start": start, "deadline": deadline},
                       "handler": hname, "gate": {"ref": gate_ref, "poll": "1s"} if gate_ref else {},
                       "timeout": "10s", "retry": {"max_attempts": 1}, "on_deadline": "missed",
                       "catchup": "run_in_window"})
    config = {"schema_version": 1, "config_version": 1, "name": "bridge-e2e",
              "timezone": "Asia/Shanghai",
              "handlers": {h: {"argv": handler, "env_allow": ["CORE_DATA_ROOT", "GUARDIAN_DRY_RUN"]}
                           for h in ("calendar_refresh", "kaipan_hot_capture", "close_capture", "intraday_data")},
              "events": events}
    config["handlers"]["alert"] = {"argv": alert, "env_allow": ["CORE_DATA_ROOT", "GUARDIAN_DRY_RUN"]}
    config["handlers"]["intraday_data"]["gate_argv"] = gate
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(config))
    state = tmp_path / "state"
    core_root = tmp_path / "core"
    cal_dir = core_root / "trade_calendar"
    cal_dir.mkdir(parents=True)
    days = []
    for offset in range(-2, 40):
        day = (now.date() + timedelta(days=offset)).isoformat()
        days.append({"d": day, "t": 1, "src": "TEST", "u": now.isoformat()})
    matrix = {"schema": "CoreTradeCalendarMatrixV1", "lit_from": days[0]["d"],
              "lit_to": days[-1]["d"], "last_refresh_at": now.isoformat(),
              "last_ok_at": now.isoformat(), "last_error": None, "conflicts": [], "days": days}
    (cal_dir / "calendar.json").write_text(json.dumps(matrix))
    sys.path.insert(0, str(ROOT / "src"))
    from engine_core.data_pipeline.storage import ArtifactCatalog
    hot_rows = [{"plate_code": f"BK{i:03d}", "requested_date": now.date().isoformat(),
                 "response_time": "09:21:00", "response_min": "09:20:00", "response_max": "09:21:00",
                 "fetched_at": now.isoformat(), "available_at": now.isoformat()} for i in range(50)]
    ArtifactCatalog(core_root).commit("hot_plates", now.date().isoformat(), hot_rows,
                                      {"quality": {"status": "READY"}}, source="e2e")
    env = os.environ.copy()
    env.update({"CORE_DATA_ROOT": str(core_root), "GUARDIAN_DRY_RUN": "1"})
    proc = subprocess.Popen([str(binary), "run", "--config", str(cfg), "--state-root", str(state)],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ledger = state / "ledger" / (now.date().isoformat() + ".jsonl")
        until = time.monotonic() + 30
        records = []
        while time.monotonic() < until:
            if ledger.exists():
                records = [json.loads(line) for line in ledger.read_text().splitlines() if line]
                states = [r for r in records if r["type"] == "INSTANCE_STATE"]
                terminal = {r["data"].get("event_id") for r in states
                            if r["data"].get("state", {}).get("status") not in {"RUNNING"}}
                if len(terminal) == 4 and any(r["type"] == "ALERT_OUT" for r in records):
                    break
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        statuses = {r["data"].get("event_id"): r["data"].get("state", {}).get("status")
                    for r in records if r["type"] == "INSTANCE_STATE"}
        assert statuses.get("calendar_refresh_0800") == "SUCCEEDED", (statuses, proc.poll(), proc.communicate(timeout=1) if proc.poll() is not None else "running")
        assert statuses.get("kaipan_hot_092100") == "SUCCEEDED", statuses
        assert statuses.get("close_mark_1505") == "SKIPPED_NA", statuses
        assert statuses.get("intraday_100005") == "GATE_BLOCKED", statuses
        assert sum(r["type"] == "ALERT_OUT" for r in records) == 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
