from pathlib import Path
import subprocess

from examples import pm, release


def test_global_invariants_cover_historical_regression():
    assert {'tests/test_data_pipeline.py', 'tests/test_registry_package.py'} <= set(release.GLOBAL_INVARIANT_TESTS)


def test_land_success_and_failed_merge_preserves_status(tmp_path, monkeypatch, capsys):
    repo = tmp_path / 'engine_core'
    repo.mkdir()
    def git(where, *args):
        return subprocess.run(['git', '-C', str(where), *args], capture_output=True,
                              text=True, check=True).stdout.strip()
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '--allow-empty', '-qm', 'base')
    git(repo, 'branch', 'C1119')
    worktree = tmp_path / 'engine_core-wt-C1119'
    git(repo, 'worktree', 'add', '-qb', 'candidate', str(worktree))
    git(worktree, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '--allow-empty', '-qm', 'candidate')
    candidate = git(worktree, 'rev-parse', 'HEAD')
    cards = tmp_path / 'cards'
    cards.mkdir()
    monkeypatch.setattr(pm, 'ROOT', repo)
    monkeypatch.setattr(pm, 'CARDS', cards)
    card = cards / 'C1119.md'
    card.write_text('STATUS: PARTIAL\n')
    calls = []
    real_run = subprocess.run
    merge_output = f'选中 8 个测试文件、共 99 个测试、耗时 1 秒\nmerged {candidate} as abcdef123456; tests ran on the merged tree\n'
    def run(command, **kwargs):
        if command[0] == pm.PYTHON:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, merge_output, '')
        return real_run(command, **kwargs)
    monkeypatch.setattr(subprocess, 'run', run)
    assert pm.main(['land', 'C1119']) == 0
    assert capsys.readouterr().out.strip() == (f'LAND C1119 using worktree commit {candidate}\n'
                                               'LAND C1119 merged abcdef123456 tests=8文件/99个')
    assert calls[0] == [pm.PYTHON, str(pm.ROOT / 'examples/release.py'), 'merge', '--branch', candidate, '--card', 'C1119', '--max-tests-files', '12']
    assert 'STATUS: DONE' in card.read_text()
    card.write_text('STATUS: PARTIAL\n')
    def failed_merge(command, **kwargs):
        if command[0] == pm.PYTHON:
            return subprocess.CompletedProcess(command, 1, 'test log: /tmp/fail.log\nE   AssertionError: broken\n  FAILED tests/test_data_pipeline.py::test_gap\n', '')
        return real_run(command, **kwargs)
    monkeypatch.setattr(subprocess, 'run', failed_merge)
    assert pm.main(['land', 'C1119']) != 0
    output = capsys.readouterr().out.strip()
    assert len(output.splitlines()) == 2 and all(x in output for x in ['using worktree commit', 'tests/test_data_pipeline.py::test_gap', 'AssertionError: broken', '/tmp/fail.log'])
    assert card.read_text() == 'STATUS: PARTIAL\n'


def test_gate_creates_requested_runner_and_runs(tmp_path, monkeypatch, capsys):
    for name in ('cards', 'reports', 'evidence'):
        (tmp_path / name).mkdir()
        monkeypatch.setenv('PM_' + name.upper() + '_DIR', str(tmp_path / name))
    monkeypatch.setattr(pm, 'try_pack', lambda card: None)
    def card_command(args):
        assert args.command in ('dispatch', 'wait-card')
        if args.command == 'wait-card':
            (tmp_path / 'reports/C987654.md').write_text('VERDICT:PASS\nGATE PASS 29 项 0 fail\n')
        return 0
    monkeypatch.setattr(pm, 'card_command', card_command)
    assert pm.main(['gate', 'C987654', '--sha', 'abcdef1', '--title', '验证', '--extra', '额外检查']) == 0
    assert capsys.readouterr().out.strip() == 'C987654 GATE PASS 29 项 0 fail VERDICT:PASS'
    card = (tmp_path / 'cards/C987654.md').read_text()
    assert all(x in card for x in ['ROLE: runner', 'abcdef1', 'CORE_STATE_CACHE=off', 'GATE_REPO', '--date 2026-11-13 --prev-date 2026-11-12 --skip-pytest', '额外检查'])
    assert pm.main(['gate', 'C987654', '--sha', 'abcdef1', '--title', '验证']) != 0
