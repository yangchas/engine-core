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
    cards = tmp_path / 'cards'
    cards.mkdir()
    card = cards / 'C999.md'
    contents = f'STATUS: ready\nSCOPE_WRITE: {repo}/tracked\n'
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
