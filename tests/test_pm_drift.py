import fcntl
import shutil
import pytest
import subprocess
from pathlib import Path

from examples import pm


@pytest.mark.live
def test_drift_compares_actual_failures_in_git_revisions(tmp_path, monkeypatch, capsys):
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git('init', '-q')
    (repo / 'tests').mkdir()
    (repo / 'tests/test_drift_sample.py').write_text('def test_fixed():\n    assert False\n\ndef test_common():\n    assert False\n')
    git('add', '.')
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    (repo / 'tests/test_drift_sample.py').write_text("import pytest\n@pytest.mark.parametrize('value', [pytest.param(1, id='with space')])\ndef test_new(value):\n    assert False\n\ndef test_common():\n    assert False\n")
    git('add', '.')
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'head')
    head = git('rev-parse', 'HEAD')
    monkeypatch.setattr(pm, 'ROOT', repo)
    monkeypatch.setattr(pm, 'DRIFT_WORKTREE_ROOT', tmp_path)
    monkeypatch.setattr(pm, 'DRIFT_LOCK', tmp_path / 'lock')
    evidence = tmp_path / 'evidence'
    assert pm.main(['drift', '--full', '--base', base, '--head', head, '--evidence-dir', str(evidence)]) == 0
    assert f'DRIFT base={base}(失败2) head={head}(失败2) new=1 fixed=1' in capsys.readouterr().out
    report = next(evidence.glob('drift-*.txt')).read_text()
    assert 'test_drift_sample.py::test_new[with space]' in report
    assert 'assert False' in report
    assert 'test_drift_sample.py::test_fixed' not in report
    assert len(git('worktree', 'list', '--porcelain').split('worktree ')) == 2


def test_drift_refuses_concurrent_pytest(tmp_path, monkeypatch, capsys):
    lock = tmp_path / 'lock'
    monkeypatch.setattr(pm, 'DRIFT_LOCK', lock)
    with lock.open('w') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert pm.main(['drift', '--base', 'HEAD', '--head', 'HEAD', '--evidence-dir', str(tmp_path)]) != 0
    assert 'lock' in capsys.readouterr().out.lower()


def test_drift_reports_incomplete_runs_and_attempts_both_revisions(tmp_path, monkeypatch, capsys):
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git('init', '-q')
    (repo / 'tests').mkdir()
    (repo / 'tests/test_sample.py').write_text('def test_sample(): pass\n')
    git('add', '.')
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'base')
    revision = git('rev-parse', 'HEAD')
    monkeypatch.setattr(pm, 'ROOT', repo)
    monkeypatch.setattr(pm, 'DRIFT_WORKTREE_ROOT', tmp_path)
    monkeypatch.setattr(pm, 'DRIFT_LOCK', tmp_path / 'lock')
    real_popen = subprocess.Popen
    calls = []
    class TimedOut:
        pid = 123
        def communicate(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired('pytest', timeout)
            return b'', None
    class CollectionError:
        returncode = 2
        def communicate(self, timeout=None):
            return b'ERROR tests/test_broken.py - collection failed\n', None
    def popen(command, **kwargs):
        if command[0] == pm.PYTHON:
            calls.append(command)
            return TimedOut() if len(calls) == 1 else CollectionError()
        return real_popen(command, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', popen)
    monkeypatch.setattr(pm.os, 'killpg', lambda *_: None)
    evidence = tmp_path / 'evidence'
    assert pm.main(['drift', '--full', '--base', revision, '--head', revision, '--evidence-dir', str(evidence)]) == 2
    assert len(calls) == 2
    assert 'comparison incomplete' in capsys.readouterr().out
    report = next(evidence.glob('drift-*.txt')).read_text()
    assert 'TIMEOUT' in report
    assert 'tests/test_broken.py - collection failed' in report


@pytest.mark.live
def test_drift_selects_affected_tests_and_skips_live(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    (repo / "examples").mkdir()
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    shutil.copy(Path(pm.__file__).with_name("affected_tests.py"), repo / "examples/affected_tests.py")
    (repo / "tests/conftest.py").write_text("import pytest\ndef pytest_collection_modifyitems(config, items):\n    for item in items:\n        if 'live' in item.keywords: item.add_marker(pytest.mark.skip(reason='live'))\n")
    (repo / "examples/changed.py").write_text("value = 1\n")
    (repo / "tests/test_selected.py").write_text("import examples.changed\nimport pytest\ndef test_common(): assert False\ndef test_fixed(): assert False\n@pytest.mark.live\ndef test_danger(): assert False\n")
    (repo / "tests/test_unrelated.py").write_text("def test_unrelated(): assert False\n")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "examples/changed.py").write_text("value = 2\n")
    (repo / "tests/test_selected.py").write_text("import examples.changed\nimport pytest\ndef test_common(): assert False\ndef test_new(): assert False\n@pytest.mark.live\ndef test_danger(): assert False\n")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "head")
    head = git("rev-parse", "HEAD")
    monkeypatch.setattr(pm, "ROOT", repo)
    monkeypatch.setattr(pm, "DRIFT_WORKTREE_ROOT", tmp_path)
    monkeypatch.setattr(pm, "DRIFT_LOCK", tmp_path / "lock")
    assert pm.main(["drift", "--base", base, "--head", head, "--evidence-dir", str(tmp_path / "evidence")]) == 0
    assert f"DRIFT base={base}(失败2) head={head}(失败2) new=1 fixed=1" in capsys.readouterr().out
