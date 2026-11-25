"""C1169: sources are the actual fw reports and core-data catalog (read-only)."""
import json
from pathlib import Path
import subprocess

import pytest
from examples import pm


def test_land_real_reports(tmp_path, monkeypatch, capsys):
    cards = tmp_path / 'cards'
    reports = tmp_path / 'reports'
    cards.mkdir()
    reports.mkdir()
    monkeypatch.setattr(pm, 'CARDS', cards)
    monkeypatch.setenv('PM_REPORTS_DIR', str(reports))
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[0] == 'git':
            return subprocess.CompletedProcess(command, 0, ('a' if command[-1].startswith('C') else 'b') * 40, '')
        return subprocess.CompletedProcess(command, 0, 'merged C1164 as abcdef12\n', '')
    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(pm, 'card_command', lambda args: 0)
    for ident in ('C1125', 'C1145', 'C1149', 'C1164'):
        (cards / f'{ident}.md').write_text('STATUS: PARTIAL\n')
        (reports / f'{ident}.md').write_bytes((Path('/home/exedev/fw/reports') / f'{ident}.md').read_bytes())
        calls.clear()
        pm.main(['land', ident])
        output = capsys.readouterr().out
        if ident == 'C1164':
            assert 'REFUSED' in output and not calls
            pm.main(['land', ident, '--force'])
            assert 'forced' in capsys.readouterr().out and calls
        else:
            assert 'REFUSED' not in output and calls


@pytest.mark.parametrize('case', ['authorize', 'catalog-retire', 'field-unify'])
def test_newcard_authorization_and_templates(case, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('PM_CARDS_DIR', str(tmp_path))
    args = ['newcard', '--recipe', 'runner-verify', '--title', '验证', '--serves', '目标', '--plan', 'P0 2/6', '--approval', '已授权', '--scope', 'examples/pm.py', '--goal', '验证问题', '--real-data', '真实卡', '--oracle', '结果一致', '--accept-by', 'real-run', '--question', '验证什么', '--dry-run']
    if case == 'authorize':
        args += ['--authorize', '允许写 Redis/TD 并联网、删键、触发事件']
    else:
        args += ['--template', case, '--function', 'limit_up_pool_today', '--tests', 'tests/test_limit_up_today.py']
    if case == 'authorize':
        assert pm.main(args) == 2
        assert 'REFUSED' in capsys.readouterr().out
        assert pm.main(args[:-2] + ['--authorize', '允许联网']) == 0
        narrow = capsys.readouterr().out
        assert '不写 Redis/TD' in narrow and '不删键' in narrow and '不触发事件' in narrow
    else:
        assert pm.main(args) == 0
        output = capsys.readouterr().out
        assert 'grep -rl limit_up_pool_today tests/' in output
        assert 'grep -rl limit_up_pool_today src/' in output
        assert '报告' in output and '验收清单' in output


def test_catalog_audit_real_catalog(monkeypatch, capsys):
    path = Path('/home/exedev/core-data/catalog.json')
    before = path.read_bytes()
    datasets = json.loads(before)['datasets']
    assert pm.main(['catalog-audit']) == 0
    output = capsys.readouterr().out
    for name, data in datasets.items():
        dates = data['dates']
        latest = max(dates)
        count = sum(len(v) for v in dates.values())
        as_of = dates[latest][-1].get('as_of') or 'UNKNOWN'
        assert any(all(s in line for s in (name, str(count), latest, as_of)) for line in output.splitlines())
    assert 'ACTIVE' in output and path.read_bytes() == before
