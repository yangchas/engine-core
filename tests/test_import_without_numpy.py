import subprocess
import sys


def test_import_engine_core_without_numpy():
    code = """
import sys
sys.modules['numpy'] = None
import engine_core
try:
    from engine_core import compute_strength
except ImportError as exc:
    assert 'plate_strength requires NumPy' in str(exc), str(exc)
else:
    raise AssertionError('compute_strength should require NumPy when accessed')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_compute_strength_remains_available_from_package():
    from engine_core import compute_strength

    assert callable(compute_strength)
