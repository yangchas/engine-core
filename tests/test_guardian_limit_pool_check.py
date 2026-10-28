"""C580: fast subset of the fake-clock acceptance check of ``limit_pool_d1`` plus the production-code mutation check."""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not Path("/bin/go").is_file(), reason="needs the Go toolchain")
def test_limit_pool_fake_clock_subset():
    work = tempfile.mkdtemp(prefix="c", dir="/tmp")  # unix socket paths must stay short
    try:
        result = subprocess.run(
            [sys.executable, "examples/guardian_limit_pool_check.py", "--only", "A,C,E,G,H,I,J", "--work-dir", work],
            cwd=ROOT, capture_output=True, text=True, timeout=500)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
    assert "passed=7/7" in result.stdout


def test_every_mutation_of_the_production_logic_is_killed():
    result = subprocess.run([sys.executable, "examples/mutation_check_limit_pool.py"], cwd=ROOT,
                            capture_output=True, text=True, timeout=500)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
    assert "survivors=0" in result.stdout
