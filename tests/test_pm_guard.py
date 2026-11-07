# DATA: spec (commands observed in the 2026-11-07 PM session)
import json
import os
from pathlib import Path
import subprocess
import sys

from examples.pm_guard import decide

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = json.loads((ROOT / 'tests/fixtures/pm_guard_samples.json').read_text())


def cases(kind):
    tool = 'Bash' if kind.startswith('bash') else 'Write'
    key = 'command' if tool == 'Bash' else 'file_path'
    return [(tool, {key: value}) for value in SAMPLES[kind]]


def test_allow_samples():
    for tool, data in cases('bash_allow') + cases('file_allow'):
        assert decide(tool, data, False)[0], data


def test_deny_samples():
    for tool, data in cases('bash_deny') + cases('file_deny'):
        allowed, reason = decide(tool, data, False)
        assert not allowed and reason, data


def test_permit():
    for tool, data in cases('bash_deny') + cases('file_deny'):
        assert decide(tool, data, True)[0]
        assert not decide(tool, data, False)[0]


def test_main(tmp_path):
    env = dict(os.environ, PM_GUARD_LOG=str(tmp_path / 'guard.log'),
               PM_GUARD_PERMIT=str(tmp_path / 'missing'))
    for kind, expected in [('bash_deny', 2), ('bash_allow', 0)]:
        tool, data = cases(kind)[0]
        result = subprocess.run([sys.executable, str(ROOT / 'examples/pm_guard.py')],
                                input=json.dumps({'tool_name': tool, 'tool_input': data}),
                                text=True, capture_output=True, env=env)
        assert result.returncode == expected
        if expected == 2:
            assert 'pm' in result.stderr
    lines = (tmp_path / 'guard.log').read_text().splitlines()
    assert len(lines) == 1 and '\tDENY\t' in lines[0]
