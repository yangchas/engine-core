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
