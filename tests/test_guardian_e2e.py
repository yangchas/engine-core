import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "examples" / "guardian_selftest.py"
GO_ROOT = ROOT / "native" / "guardiand"


def test_guardian_wall_clock_selftest_fast_suite():
    go = Path("/bin/go")
    if not go.is_file():
        pytest.fail("/bin/go is missing; guardian self-test must not silently skip")
    if not (GO_ROOT / "go.mod").is_file():
        pytest.fail(f"{GO_ROOT}/go.mod is missing; guardian self-test must not silently skip")
    work_dir = Path(tempfile.mkdtemp(prefix="guardian-selftest-pytest-"))
    process = subprocess.Popen(
        [sys.executable, str(DRIVER), "--mode", "fast", "--work-dir", str(work_dir)],
        cwd=ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=10)
        pytest.fail(f"guardian fast self-test exceeded 120s\n{stdout}\n{stderr}")
    print(stdout)
    if stderr:
        print(stderr, file=sys.stderr)
    assert process.returncode == 0, f"guardian self-test failed ({process.returncode})\n{stdout}\n{stderr}"


def test_guardian_schedule_at_list_expands_trigger_instance_names():
    probe = """
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "examples"))
import guardian_selftest as module

schedule = json.loads((root / "src" / "engine_core" / "guardian" / "schedule.json").read_text())
events = module._effective_schedule_instances(schedule)
print(json.dumps({
    "instances": [(event["instance_id"], event["start"]) for event in events if event["source_event_id"] == "daily_kline_backfill"],
    "legacy": module._compare_legacy_schedule(schedule),
}, separators=(",", ":")))
"""
    process = subprocess.run(
        [sys.executable, "-c", probe, str(ROOT)], cwd=ROOT,
        capture_output=True, text=True, check=False,
    )
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == '{"instances":[["daily_kline_backfill@01:00","01:00:00"],["daily_kline_backfill@08:00","08:00:00"]],"legacy":[62,57]}'
