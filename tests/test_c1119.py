from pathlib import Path
import subprocess

from examples import pm, release


def test_global_invariants_cover_historical_regression():
    assert {'tests/test_data_pipeline.py', 'tests/test_registry_package.py'} <= set(release.GLOBAL_INVARIANT_TESTS)


def test_land_success_and_failed_merge_preserves_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('PM_CARDS_DIR', str(tmp_path))
    card = tmp_path / 'C1119.md'
    card.write_text('STATUS: PARTIAL\n')
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, '选中 8 个测试文件、共 99 个测试、耗时 1 秒\nmerged C1119 as abcdef123456; tests ran on the merged tree\n', '')
    monkeypatch.setattr(pm.subprocess, 'run', run)
    assert pm.main(['land', 'C1119']) == 0
    assert capsys.readouterr().out.strip() == 'LAND C1119 merged abcdef123456 tests=8文件/99个'
    assert calls[0] == [pm.PYTHON, str(pm.ROOT / 'examples/release.py'), 'merge', '--branch', 'C1119', '--card', 'C1119', '--max-tests-files', '12']
    assert 'STATUS: DONE' in card.read_text()
    card.write_text('STATUS: PARTIAL\n')
    monkeypatch.setattr(pm.subprocess, 'run', lambda command, **kwargs: subprocess.CompletedProcess(command, 1, 'test log: /tmp/fail.log\nE   AssertionError: broken\n  FAILED tests/test_data_pipeline.py::test_gap\n', ''))
    assert pm.main(['land', 'C1119']) != 0
    output = capsys.readouterr().out.strip()
    assert len(output.splitlines()) == 1 and all(x in output for x in ['tests/test_data_pipeline.py::test_gap', 'AssertionError: broken', '/tmp/fail.log'])
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
