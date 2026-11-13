import pytest
from examples import pm
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


def test_card_listing_and_status(tmp_path, monkeypatch, capsys):
    import shutil
    cards = tmp_path / 'cards'
    shutil.copytree(Path(__file__).parent / 'fixtures/pm_cards', cards)
    monkeypatch.setenv('PM_CARDS_DIR', str(cards))
    monkeypatch.setenv('PM_REPORTS_DIR', str(tmp_path / 'reports'))
    monkeypatch.setenv('PM_RUNS_DIR', str(tmp_path / 'runs'))
    assert main(['cards', '--limit', '2']) == 0
    output = capsys.readouterr().out
    assert 'C1047' in output and 'C1046' in output and 'C1045' not in output
    assert main(['status', 'C1045']) == 0
    output = capsys.readouterr().out
    assert 'STATUS:' in output and 'ROLE:' in output and 'EXECUTOR:' in output
    assert main(['status', 'C999999']) == 2
    assert 'C104' in capsys.readouterr().out


def test_dispatch_workflow(tmp_path, monkeypatch, capsys):
    import shutil
    import examples.pm as pm
    cards = tmp_path / 'cards'
    shutil.copytree(Path(__file__).parent / 'fixtures/pm_cards', cards)
    monkeypatch.setenv('PM_CARDS_DIR', str(cards))
    monkeypatch.setenv('PM_RUNS_DIR', str(tmp_path / 'runs'))
    preflight = tmp_path / 'preflight'
    preflight.write_text('#!/bin/sh\necho PREFLIGHT OK\n')
    preflight.chmod(0o755)
    runner = tmp_path / 'runner'
    runner.write_text('#!/bin/sh\nexit 0\n')
    runner.chmod(0o755)
    monkeypatch.setenv('PM_PREFLIGHT', str(preflight))
    monkeypatch.setenv('PM_RUNCARD', str(runner))
    monkeypatch.setattr(pm, 'card_processes', lambda card: [])
    card = cards / 'C1045.md'
    card.write_text(pm.set_status(card.read_text(), 'STATUS: ready'))
    assert main(['dispatch', 'C1045']) == 0
    assert 'dispatched C1045' in capsys.readouterr().out
    assert (tmp_path / 'runs/C1045.dispatch.out').exists()
    assert pm.field(card.read_text(), 'STATUS').startswith('dispatched(')
    assert (tmp_path / 'runs/C1045.dispatch.timestamp').exists()
    preflight.write_text('#!/bin/sh\necho preflight-failed\nexit 1\n')
    assert main(['dispatch', 'C1045']) == 2
    assert 'preflight-failed' in capsys.readouterr().out
    preflight.write_text('#!/bin/sh\necho PREFLIGHT OK\n')
    monkeypatch.setattr(pm, 'card_processes', lambda card: [1])
    assert main(['dispatch', 'C1045']) == 2
    monkeypatch.setattr(pm, 'card_processes', lambda card: [])
    card.write_text(pm.set_status(card.read_text(), 'STATUS: done'))
    assert main(['dispatch', 'C1045']) == 2


def test_setstatus_and_wait_card(tmp_path, monkeypatch, capsys):
    import shutil
    import examples.pm as pm
    cards = tmp_path / 'cards'
    shutil.copytree(Path(__file__).parent / 'fixtures/pm_cards', cards)
    monkeypatch.setenv('PM_CARDS_DIR', str(cards))
    monkeypatch.setenv('PM_REPORTS_DIR', str(tmp_path / 'reports'))
    monkeypatch.setenv('PM_RUNS_DIR', str(tmp_path / 'runs'))
    assert main(['setstatus', 'C1045', 'ready test']) == 0
    assert 'STATUS: ready test' in capsys.readouterr().out
    monkeypatch.setattr(pm, 'card_processes', lambda card: [1])
    assert main(['wait-card', 'C1045', '--timeout', '0']) == 2
    assert 'TIMEOUT' in capsys.readouterr().out
    states = iter([[1], [], []])
    monkeypatch.setattr(pm, 'card_processes', lambda card: next(states))
    assert main(['wait-card', 'C1045', '--timeout', '1', '--interval', '0.001']) == 0
    output = capsys.readouterr().out
    assert 'running' in output and 'exited' in output


def test_newcard_real_cards(tmp_path, monkeypatch, capsys):
    import shutil
    from examples.card_lint import lint_card
    cards = tmp_path / 'cards'
    shutil.copytree(Path(__file__).parent / 'fixtures/pm_cards', cards)
    for p in (Path(__file__).parent / 'fixtures/card_lint').glob('C*.md'):
        if p.stem != 'C1056':
            shutil.copy(p, cards / p.name)
    monkeypatch.setenv('PM_CARDS_DIR', str(cards))
    monkeypatch.setenv('PM_NO_WORKTREE', '1')
    args = ['newcard', '--recipe', 'builder-fix', '--title', '修复', '--serves', '目标',
            '--plan', 'P0 2/6', '--approval', '已授权', '--scope', 'examples/pm.py',
            '--goal', '修复问题', '--real-data', '真实卡', '--oracle', '结果一致',
            '--accept-by', 'fail-first-real-fixture', '--dry-run']
    assert main(args) == 0
    output = capsys.readouterr().out
    assert 'ID: C1048' in output
    assert not any(x.startswith('FAIL') for x in lint_card(output, Path(__file__).parents[1]))
    from examples.pm import field
    assert all(field(output, name) for name in ('REAL_DATA', 'ORACLE', 'ACCEPT_BY', 'TEST_BUDGET', 'STUBS_ALLOWED'))
    before = set(cards.iterdir())
    assert main(['newcard', '--recipe', 'builder-fix']) == 2
    assert set(cards.iterdir()) == before


def test_testlist_oracle(capsys):
    assert main(['testlist', '--files', 'examples/pm.py,examples/card_lint.py,tests/test_pm.py']) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[:-1] == ['tests/test_pm.py', 'tests/test_card_lint.py', 'tests/test_real_fixtures.py',
                          'tests/test_trade_day_context.py', 'tests/test_registry_storage.py',
                          'tests/test_guardian_contract_static.py', 'tests/test_dataset_registry.py',
                          'tests/test_baseline_window_declaration.py']


def test_dispatch_lint_real_card(tmp_path, monkeypatch, capsys):
    import examples.pm as pm
    cards = tmp_path / 'cards'
    cards.mkdir()
    text = (Path(__file__).parent / 'fixtures/card_lint/C1046.md').read_text()
    text = text.replace('C1046', 'C1099')
    (cards / 'C1099.md').write_text(pm.set_status(text, 'STATUS: ready'))
    monkeypatch.setenv('PM_CARDS_DIR', str(cards))
    assert main(['dispatch', 'C1099']) == 2
    assert 'FAIL required_fields' in capsys.readouterr().out
    main(['dispatch', 'C1099', '--no-lint'])
    assert 'FAIL required_fields' not in capsys.readouterr().out


def test_c1055_newcard_and_new_files(tmp_path, monkeypatch, capsys):
    import shutil
    cards = tmp_path / 'cards'
    shutil.copytree(Path(__file__).parent / 'fixtures/pm_runs/cards', cards)
    monkeypatch.setenv('PM_CARDS_DIR', str(cards))
    monkeypatch.setenv('PM_REPORTS_DIR', str(tmp_path / 'reports'))
    monkeypatch.setenv('PM_EVIDENCE_DIR', str(tmp_path / 'evidence'))
    args = ['--title', '修复', '--serves', '目标', '--plan', 'P0 4/6', '--approval', '已授权',
            '--scope', 'examples/new_module.py,tests/test_new_module.py,tests/test_other_new.py',
            '--goal', '修复', '--real-data', '真实卡', '--oracle', '结果一致',
            '--accept-by', 'fail-first-real-fixture', '--dry-run']
    assert main(['newcard', '--recipe', 'builder-fix', *args]) == 0
    output = capsys.readouterr().out
    assert 'COMMIT: yes' in output
    assert str(tmp_path / 'reports/C1055.md') in output
    assert str(tmp_path / 'evidence/C1055') in output
    assert main(['newcard', '--recipe', 'runner-verify', '--question', '验证什么?', *args]) == 0
    assert 'COMMIT: no' in capsys.readouterr().out
    card = cards / 'C1054.md'
    text = card.read_text()
    import re
    text = re.sub(r'^SCOPE_WRITE:.*', 'SCOPE_WRITE: /home/exedev/repos/engine_core-wt-C1055/examples/new_module.py, /home/exedev/repos/engine_core-wt-C1055/tests/test_other_new.py', text, flags=re.M)
    card.write_text(text)
    assert main(['testlist', '--card', 'C1054']) == 0
    output = capsys.readouterr().out
    assert 'tests/test_new_module.py' in output and 'tests/test_other_new.py' in output


def test_c1055_wait_lifecycle_real_report(tmp_path, monkeypatch, capsys):
    import os
    import shutil
    import examples.pm as pm
    source = Path(__file__).parent / 'fixtures/pm_runs'
    shutil.copytree(source / 'cards', tmp_path / 'cards')
    shutil.copytree(source / 'reports', tmp_path / 'reports')
    (tmp_path / 'runs').mkdir()
    shutil.copy2(source / 'C1054.dispatch.out', tmp_path / 'runs/C1054.dispatch.out')
    for key, name in [('PM_CARDS_DIR', 'cards'), ('PM_REPORTS_DIR', 'reports'), ('PM_RUNS_DIR', 'runs')]:
        monkeypatch.setenv(key, str(tmp_path / name))
    card = tmp_path / 'cards/C1054.md'
    card.write_text(pm.set_status(card.read_text(), 'STATUS: dispatched(2026-11-13)'))
    report = tmp_path / 'reports/C1054.md'
    dispatch = tmp_path / 'runs/C1054.dispatch.out'
    os.utime(report, (dispatch.stat().st_mtime - 60,) * 2)
    monkeypatch.setattr(pm, 'card_processes', lambda _: [1])
    clock = iter([0, 0, 31, 32])
    monkeypatch.setattr(pm.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(pm.time, 'sleep', lambda _: None)
    assert main(['wait-card', 'C1054', '--timeout', '31', '--interval', '1']) == 2
    assert 'TIMEOUT' in capsys.readouterr().out
    monkeypatch.setattr(pm, 'card_processes', lambda _: [])
    monkeypatch.setattr(pm.time, 'monotonic', lambda: 0)
    assert main(['wait-card', 'C1054', '--timeout', '1']) == 0
    assert pm.field(card.read_text(), 'STATUS').startswith('dispatched')
    os.utime(report, (dispatch.stat().st_mtime + 60,) * 2)
    assert main(['wait-card', 'C1054', '--timeout', '1']) == 0
    assert pm.field(card.read_text(), 'STATUS').startswith('PARTIAL(自动;')
    card.write_text(pm.set_status(card.read_text(), 'STATUS: dispatched(2026-11-13)'))
    monkeypatch.setattr(pm, 'card_processes', lambda _: [1])
    assert main(['wait-card', 'C1054', '--timeout', '0']) == 2
    assert pm.field(card.read_text(), 'STATUS').startswith('dispatched')
    monkeypatch.setattr(pm, 'card_processes', lambda _: [])
    card.write_text(pm.set_status(card.read_text(), 'STATUS: ready 手动'))
    assert main(['wait-card', 'C1054', '--timeout', '1']) == 0
    assert pm.field(card.read_text(), 'STATUS') == 'ready 手动'


def test_c1055_metrics_real_archives(tmp_path, monkeypatch, capsys):
    import shutil
    source = Path(__file__).parent / 'fixtures/pm_runs'
    shutil.copytree(source / 'cards', tmp_path / 'cards')
    shutil.copytree(source / 'reports', tmp_path / 'reports')
    shutil.copytree(source / 'archive', tmp_path / 'runs/archive')
    for key, name in [('PM_CARDS_DIR', 'cards'), ('PM_REPORTS_DIR', 'reports'), ('PM_RUNS_DIR', 'runs')]:
        monkeypatch.setenv(key, str(tmp_path / name))
    assert main(['metrics']) == 0
    output = capsys.readouterr().out
    assert 'C1047:7' in output and 'C1054:2' in output and 'C1049:1' in output
    assert '目标:一次通过率≥70%;平均派发≤1.5;陈旧 ready=0;DENY<5/天' in output
    assert len(output.splitlines()) <= 12


def test_c1057_runner_tiers_and_builder(tmp_path, monkeypatch, capsys):
    from examples.card_lint import lint_card
    from examples.pm import field
    cards = tmp_path / 'cards'
    cards.mkdir()
    import shutil
    shutil.copy(Path(__file__).parent / 'fixtures/card_lint/C1056.md', cards / 'C1056.md')
    monkeypatch.setenv('PM_CARDS_DIR', str(cards))
    args = ['--title', '探针', '--serves', '目标', '--plan', 'P0 5/6', '--approval', '已授权',
            '--goal', '观察', '--real-data', '真实卡', '--oracle', '结果一致',
            '--accept-by', 'real-run', '--dry-run']
    assert main(['newcard', '--recipe', 'runner-observe', '--tier', 'easy', '--question', '模型是什么?', *args]) == 0
    output = capsys.readouterr().out
    assert field(output, 'MODEL') == 'gpt-5.6-luna' and field(output, 'EFFORT') == 'low'
    assert field(output, 'QUESTION') and field(output, 'NEED_DATA')
    assert '/home/exedev/fw/reports/C1057.md' in field(output, 'SCOPE_WRITE')
    assert '/home/exedev/fw/evidence/C1057' in field(output, 'SCOPE_WRITE')
    assert 'TEST_BUDGET:' not in output
    assert not any(x.startswith('FAIL') for x in lint_card(output, Path(__file__).parents[1]))
    assert main(['newcard', '--recipe', 'runner-observe', *args]) == 2
    assert 'question' in capsys.readouterr().out
    assert main(['newcard', '--recipe', 'builder-framework', '--tier', 'hard', '--scope', 'examples/pm.py', *args]) == 0
    output = capsys.readouterr().out
    assert field(output, 'MODEL') == 'gpt-6.1-terra' and field(output, 'EFFORT') == 'high'
    assert field(output, 'COMMIT') == 'yes'


def test_pack_real_c1047_constraints_and_idempotency(tmp_path, monkeypatch):
    import shutil
    cards = tmp_path / 'cards'
    cards.mkdir()
    shutil.copy(Path(__file__).parent / 'fixtures/card_lint/C1047.md', cards / 'C1047.md')
    monkeypatch.setenv('PM_CARDS_DIR', str(cards))
    for _ in range(2):
        assert main(['pack', 'C1047', '--out-dir', str(tmp_path)]) == 0
    result = (tmp_path / 'C1047.md').read_text()
    assert 'td_stops_0945' in result and 't1v2_restart_zeroes_anchors' in result
    assert len(result.splitlines()) <= 150
    assert (cards / 'C1047.md').read_text().count('\nCONTEXT:') == 1


def test_pack_real_c1046_code_map(tmp_path, monkeypatch):
    import ast
    import re
    repo = Path(__file__).resolve().parents[1]
    cards = tmp_path / 'cards'
    cards.mkdir()
    text = (Path(__file__).parent / 'fixtures/pm_cards/C1046.md').read_text()
    # Real card header, with scope redirected to the worktree under test.
    text = re.sub(r'^SCOPE_WRITE:.*', f'SCOPE_WRITE: {repo}/examples/pm_guard.py, {repo}/tests/test_pm_guard.py', text, flags=re.M)
    (cards / 'C1046.md').write_text(text)
    monkeypatch.setenv('PM_CARDS_DIR', str(cards))
    assert main(['pack', 'C1046', '--out-dir', str(tmp_path)]) == 0
    result = (tmp_path / 'C1046.md').read_text()
    nodes = ast.parse((repo / 'examples/pm_guard.py').read_text()).body
    for name in ('decide', '_bash'):
        node = next(n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
        assert re.search(rf'{name}\(.*\).*:{node.lineno}\b', result)
    assert 'tests/test_pm_guard.py' in result


def test_pack_real_precedents_and_dangerous_control(tmp_path, monkeypatch):
    import shutil
    cards = tmp_path / 'cards'
    shutil.copytree(Path(__file__).parent / 'fixtures/pm_cards/precedents', cards)
    monkeypatch.setenv('PM_CARDS_DIR', str(cards))
    assert main(['pack', 'C1053', '--out-dir', str(tmp_path)]) == 0
    text = (tmp_path / 'C1053.md').read_text()
    section = text.split('先例', 1)[1].split('环境事实', 1)[0]
    assert any(card in section for card in ('C1049', 'C1050', 'C1052', 'C1054', 'C1055'))
    assert 'C1051|' not in section  # BLOCKED is a dangerous false precedent.


def test_staff_pack_real_context(tmp_path, monkeypatch):
    import shutil
    fixtures = Path(__file__).parent / 'fixtures'
    cards = tmp_path / 'cards'
    shutil.copytree(fixtures / 'pm_cards/c1059', cards)
    card = cards / 'C1039.md'
    card.write_text(card.read_text() + '\nEPIC: seal-order\nDEPENDS: C1055\n')
    monkeypatch.setenv('PM_CARDS_DIR', str(cards))
    monkeypatch.setenv('PM_STAFF_DIR', str(fixtures / 'pm_staff'))
    monkeypatch.setenv('PM_EPIC_DIR', str(fixtures / 'pm_staff'))
    reports = tmp_path / 'reports'
    reports.mkdir()
    shutil.copy(cards / 'C1055-report.md', reports / 'C1055.md')
    monkeypatch.setenv('PM_REPORTS_DIR', str(reports))
    assert main(['pack', 'C1039', '--out-dir', str(tmp_path)]) == 0
    text = (tmp_path / 'C1039.md').read_text()
    assert '如实汇报' in text and '夹具规则要求只能用 capture_real_fixture.py 采集' in text
    assert '买一量×价=封单' in text and '无 HANDOFF,取末 8 行' in text
    assert len(text.splitlines()) <= 150
    assert len(text.split('## 岗位记忆')[1].splitlines()) <= 55


def test_lesson_real_experience(tmp_path, monkeypatch, capsys):
    import shutil
    staff = tmp_path / 'staff'
    shutil.copytree(Path(__file__).parent / 'fixtures/pm_staff', staff)
    monkeypatch.setenv('PM_STAFF_DIR', str(staff))
    path = staff / 'builder/EXPERIENCE.md'
    before = path.read_text().splitlines()
    args = ['lesson', 'add', '--role', 'builder', '--card', 'C1039', '--mistake', '夹具来源', '--rule', '核对来源']
    assert main(args) == 0
    assert len(path.read_text().splitlines()) == len(before) + 1
    assert path.read_text().splitlines()[-1].endswith('|C1039|夹具来源|核对来源')
    assert main(args) == 0 and len(path.read_text().splitlines()) == len(before) + 1
    assert '跳过' in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        main(['lesson', 'add', '--role', '../builder', '--card', 'C1039', '--mistake', 'x', '--rule', 'y'])
    assert error.value.code == 2


def test_scorecard_real_usage(monkeypatch, capsys):
    fixtures = Path(__file__).parent / 'fixtures'
    runs = fixtures / 'pm_runs'
    monkeypatch.setenv('PM_CARDS_DIR', str(runs))
    monkeypatch.setenv('PM_RUNS_DIR', str(runs))
    monkeypatch.setenv('PM_STAFF_DIR', str(fixtures / 'pm_staff'))
    cards = {}
    for p in runs.glob('C*.md'):
        content = p.read_text()
        cards[p.stem] = {key: pm.field(content, key) for key in ('ROLE', 'MODEL', 'EFFORT')}
    usage_rows = [line.split('\t') for line in (runs / 'usage.tsv').read_text().splitlines()]
    inferred = {row[0]: row[1:4] for row in usage_rows if len(row) == 5}
    matching = [c for c, v in cards.items() if tuple(v[k] or inferred.get(c, ['?', '?', '?'])[i] for i, k in enumerate(('ROLE', 'MODEL', 'EFFORT'))) == ('builder', 'gpt-6.1-terra', 'medium')]
    numbers = [int(row[4]) for line in (runs / 'usage.tsv').read_text().splitlines() if len(row := line.split('\t')) == 5 and row[0] in matching and tuple(row[1:4]) == ('builder', 'gpt-6.1-terra', 'medium') and row[4].isdigit()]
    assert main(['scorecard']) == 0
    output = capsys.readouterr().out
    row = next(line for line in output.splitlines() if 'builder' in line and 'gpt-6.1-terra' in line and 'medium' in line)
    assert f'卡数={len(matching)}' in row and f'平均 tokens={sum(numbers) / len(numbers):.0f}' in row
    assert '缺数据' in output and len(output.splitlines()) <= 14


def test_c1061_reconcile_real_status_and_report(tmp_path, monkeypatch, capsys):
    import shutil
    lines = (Path(__file__).parent / 'fixtures/pm_status/status_lines.txt').read_text().splitlines()
    ready = next(line for line in lines if line.lower().startswith('status: ready'))
    dispatched = next(line for line in lines if line.lower().startswith('status: dispatched'))
    done = next(line for line in lines if line.startswith('STATUS: DONE'))
    cards, reports = tmp_path / 'cards', tmp_path / 'reports'
    cards.mkdir(); reports.mkdir()
    for number, line in enumerate((ready, dispatched, ready, done), 1):
        (cards / f'C{number}.md').write_text(line + '\n')
    source = Path(__file__).parent / 'fixtures/pm_runs/reports/C1054.md'
    shutil.copyfile(source, reports / 'C1.md')
    shutil.copyfile(source, reports / 'C2.md')
    (reports / 'C3.md').touch()
    shutil.copyfile(source, reports / 'C4.md')
    monkeypatch.setenv('PM_CARDS_DIR', str(cards)); monkeypatch.setenv('PM_REPORTS_DIR', str(reports))
    before = {p.name: p.read_bytes() for p in cards.iterdir()}
    assert main(['reconcile']) == 0
    assert '陈旧卡: 2' in capsys.readouterr().out
    assert before == {p.name: p.read_bytes() for p in cards.iterdir()}
    assert main(['reconcile', '--apply']) == 0
    capsys.readouterr()
    after = {p.name: p.read_bytes() for p in cards.iterdir()}
    assert after['C3.md'] == before['C3.md'] and after['C4.md'] == before['C4.md']
    assert main(['reconcile', '--apply']) == 0
    assert '陈旧卡: 0' in capsys.readouterr().out
    assert after == {p.name: p.read_bytes() for p in cards.iterdir()}


def test_c1061_metrics_real_status_lines(tmp_path, monkeypatch, capsys):
    import re
    lines = (Path(__file__).parent / 'fixtures/pm_status/status_lines.txt').read_text().splitlines()
    cards = tmp_path / 'cards'; cards.mkdir()
    for number, line in enumerate(lines):
        (cards / f'C{number}.md').write_text(line + '\n')
    monkeypatch.setenv('PM_CARDS_DIR', str(cards))
    monkeypatch.setenv('PM_REPORTS_DIR', str(tmp_path / 'reports'))
    monkeypatch.setenv('PM_RUNS_DIR', str(tmp_path / 'runs'))
    assert main(['metrics']) == 0
    other = int(re.search(r'其他=(\d+)', capsys.readouterr().out).group(1))
    assert other / len(lines) <= 0.10
