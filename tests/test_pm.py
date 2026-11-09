from pathlib import Path

from examples.pm import TERMINAL, latest_state, main

REAL_LINES = (Path(__file__).parent / 'fixtures/real/ledger_instance_state_20261106.jsonl').read_text().splitlines()


def test_latest_state():
    event = 'td_resource_check_0825'
    running = latest_state(REAL_LINES[:2], event)
    assert running['status'] == 'RUNNING' and running['status'] not in TERMINAL
    assert latest_state(REAL_LINES, event)['status'] == 'SUCCEEDED_DEGRADED'
    assert latest_state(REAL_LINES, 'absent') is None


def test_wait_timeout(tmp_path, capsys):
    ledger = tmp_path / 'state/ledger/2026-11-06.jsonl'
    ledger.parent.mkdir(parents=True)
    ledger.write_text('\n'.join(REAL_LINES) + '\n')
    assert main(['wait', '--ledger-event', 'absent', '--date', '2026-11-06',
                 '--timeout', '1', '--interval', '1', '--state-root', str(tmp_path)]) == 2
    assert capsys.readouterr().out.startswith('TIMEOUT absent')


def test_worktree_from_real_card_headers():
    from examples.pm import worktree_from_card
    # DATA: real:fw/cards SCOPE_WRITE lines
    assert worktree_from_card('SCOPE_WRITE: /home/exedev/repos/engine_core-wt-C751/tests/fixtures/real/ledger_instance_state_20261106.jsonl, /home/exedev/repos/engine_core-wt-C751/tests/fixtures/real/ledger_instance_state_PROVENANCE.json, /home/exedev/repos/engine_core-wt-C751/examples/pm_guard.py, /home/exedev/repos/engine_core-wt-C751/examples/pm.py, /home/exedev/repos/engine_core-wt-C751/tests/test_pm_guard.py, /home/exedev/repos/engine_core-wt-C751/tests/test_pm.py, /home/exedev/repos/engine_core-wt-C751/tests/fixtures/pm_guard_samples.json') == "/home/exedev/repos/engine_core-wt-C751"
    assert worktree_from_card('SCOPE_WRITE: /home/exedev/repos/engine_core-wt-C757/tests/test_real_fixtures.py, /home/exedev/repos/engine_core-wt-C757/tests/fixtures/real/PROVENANCE.json, /home/exedev/repos/engine_core-wt-C757/src/engine_core/data_pipeline/td_sink.py, /home/exedev/repos/engine_core-wt-C757/src/engine_core/guardian/daily_kline_coverage.py, /home/exedev/repos/engine_core-wt-C757/tests/test_daily_kline_td_selfcheck.py, /home/exedev/repos/engine_core-wt-C757/tests/fixtures/real/daily_kline_c756') == "/home/exedev/repos/engine_core-wt-C757"


def test_set_status_changes_only_first_line():
    from examples.pm import set_status
    text = 'title\nSTATUS: ready\nbody\nSTATUS: later\n'
    assert set_status(text, 'STATUS: DONE') == 'title\nSTATUS: DONE\nbody\nSTATUS: later\n'


def test_accept_forwards_deep_and_prints_summary_last(tmp_path, monkeypatch, capsys):
    import subprocess
    from types import SimpleNamespace
    import examples.pm as pm

    cards = tmp_path / 'cards'
    cards.mkdir()
    (cards / 'C999.md').write_text('STATUS: ready\nSCOPE_WRITE: /tmp/engine_core-wt-C999/a\n')
    monkeypatch.setattr(pm, 'CARDS', cards)
    summary = '选中 4 个测试文件、共 12 个测试、耗时 2.50 秒; --deep=yes'
    def run(command, **kwargs):
        if command[0] == 'git':
            key = command[3:]
            value = ('builder' if key[:2] == ['branch', '--show-current'] else
                     'worktree /tmp/main' if key[:2] == ['worktree', 'list'] else
                     'main' if key[0] == 'symbolic-ref' else
                     'base' if key[0] == 'merge-base' else
                     'changed.py' if key[0] == 'diff' else 'abc123')
            return SimpleNamespace(returncode=0, stdout=value, stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(subprocess, 'run', run)
    def merge(command):
        assert '--deep' in command
        return SimpleNamespace(returncode=0, stdout=summary + '\nmerged\n', stderr='')
    monkeypatch.setattr(pm, 'run_merge', merge)
    assert pm.main(['accept', 'C999', '--no-audit', '--deep']) == 0
    assert capsys.readouterr().out.splitlines()[-1] == summary


def test_run_merge_forwards_waiting_notice(capsys):
    import sys
    from examples.pm import run_merge
    result = run_merge([sys.executable, '-c', "print('waiting for another pytest run', flush=True); print('done')"])
    assert result.returncode == 0 and result.stdout.endswith('done\n')
    assert capsys.readouterr().out == 'waiting for another pytest run\n'


def test_accept_dry_run(tmp_path, monkeypatch, capsys):
    import subprocess
    import examples.pm as pm

    repo = tmp_path / 'engine_core-wt-C999'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              capture_output=True, text=True).stdout
    git('init', '-q')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    (repo / 'tracked').write_text('original\n')
    git('add', 'tracked')
    git('commit', '-qm', 'initial')
    worktree = tmp_path / 'engine_core-wt-C998'
    git('worktree', 'add', '-qb', 'builder', str(worktree))
    (worktree / 'tracked').write_text('changed\n')
    subprocess.run(['git', '-C', str(worktree), 'commit', '-qam', 'builder change'], check=True)
    cards = tmp_path / 'cards'
    cards.mkdir()
    card = cards / 'C999.md'
    contents = f'STATUS: ready\nSCOPE_WRITE: {worktree}/tracked\n'
    card.write_text(contents)
    monkeypatch.setattr(pm, 'ROOT', repo)
    monkeypatch.setattr(pm, 'CARDS', cards)
    before = git('status', '--porcelain')
    assert pm.main(['accept', 'C999', '--dry-run']) == 0
    output = capsys.readouterr().out
    assert all(step in output for step in ('worktree', 'audit', 'merge', 'status', 'callback'))
    assert card.read_text() == contents
    assert (repo / 'tracked').read_text() == 'original\n'
    assert git('status', '--porcelain') == before


def test_accept_committed_branch_uses_main_worktree_base(tmp_path, monkeypatch, capsys):
    import subprocess
    import examples.pm as pm

    repo = tmp_path / 'main'
    repo.mkdir()
    def git(repo_path, *args):
        return subprocess.run(['git', '-C', str(repo_path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.name', 'Test')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    (repo / 'tracked').write_text('original\n')
    git(repo, 'add', 'tracked')
    git(repo, 'commit', '-qm', 'initial')
    base = git(repo, 'rev-parse', 'HEAD')
    worktree = tmp_path / 'engine_core-wt-C999'
    git(repo, 'worktree', 'add', '-qb', 'builder', str(worktree))
    (worktree / 'tracked').write_text('changed\n')
    git(worktree, 'commit', '-qam', 'builder change')
    cards = tmp_path / 'cards'
    cards.mkdir()
    (cards / 'C999.md').write_text(f'STATUS: ready\nSCOPE_WRITE: {worktree}/tracked\n')
    monkeypatch.setattr(pm, 'ROOT', worktree)
    monkeypatch.setattr(pm, 'CARDS', cards)
    assert pm.main(['accept', 'C999', '--dry-run']) == 0
    assert f' base {base}\n' in capsys.readouterr().out


def test_accept_refuses_empty_diff(tmp_path, monkeypatch, capsys):
    import subprocess
    import examples.pm as pm

    repo = tmp_path / 'main'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git('init', '-q')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    (repo / 'tracked').write_text('original\n')
    git('add', 'tracked')
    git('commit', '-qm', 'initial')
    base = git('rev-parse', 'HEAD')
    worktree = tmp_path / 'engine_core-wt-C999'
    git('worktree', 'add', '-qb', 'builder', str(worktree))
    cards = tmp_path / 'cards'
    cards.mkdir()
    (cards / 'C999.md').write_text(f'STATUS: ready\nSCOPE_WRITE: {worktree}/tracked\n')
    monkeypatch.setattr(pm, 'ROOT', repo)
    monkeypatch.setattr(pm, 'CARDS', cards)
    assert pm.main(['accept', 'C999', '--dry-run']) == 2
    assert capsys.readouterr().out == f'ACCEPT C999 REFUSED setup: empty diff vs base {base}\n'
