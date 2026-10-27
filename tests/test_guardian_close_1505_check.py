"""C566: run a fast subset of the fake-clock close-1505 acceptance check (isolated guardiand, no network)."""
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not Path("/bin/go").is_file(), reason="needs the Go toolchain")
def test_close_1505_fake_clock_subset():
    work = tempfile.mkdtemp(prefix="c", dir="/tmp")  # unix socket paths must stay short
    try:
        result = subprocess.run(
            [sys.executable, "examples/guardian_close_1505_check.py", "--only", "A,C,E,G,I,K",
             "--work-dir", work],
            cwd=ROOT, capture_output=True, text=True, timeout=400)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
    assert "passed=6/6" in result.stdout
