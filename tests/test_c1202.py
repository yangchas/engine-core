import json
from pathlib import Path

from examples.pm import main


def test_real_migration_survey_refuses_hot_rank_and_accepts_override(tmp_path, monkeypatch, capsys):
    fixture = json.loads((Path(__file__).resolve().parents[1] / 'tests/fixtures/c1203/survey-results.json').read_text())
    monkeypatch.setattr('examples.pm.migration_survey', lambda repo, name: tuple(
        fixture['results'][name][key] for key in ('kind', 'writers', 'readers')))
    cards = tmp_path / 'cards'
    cards.mkdir()
    monkeypatch.setenv('PM_CARDS_DIR', str(cards))
    common = ['newcard', '--recipe', 'builder-fix', '--title', '迁移', '--serves', '目标',
              '--plan', 'P0 流程工具修复', '--approval', '已授权', '--scope', 'examples/pm.py',
              '--real-data', '真实数据', '--oracle', '验证', '--accept-by', 'fail-first-real-fixture',
              '--template', 'catalog-retire', '--tests', 'tests/test_pm.py', '--no-worktree', '--no-pack']
    assert main([*common, '--function', 'hot_rank']) != 0
    output = capsys.readouterr().out
    assert 'hot_rank' in output and 'kind=' in output and not list(cards.iterdir())
    assert main([*common, '--function', 'hot_rank', '--survey', str(tmp_path / 'missing')]) != 0
    assert not list(cards.iterdir())
    survey = tmp_path / 'survey'
    survey.write_text('SURVEY\n')
    assert main([*common, '--function', 'hot_rank', '--survey', str(survey)]) == 0
    card = next(cards.glob('C*.md')).read_text()
    assert 'kind=' in card and '写端' in card and '读端' in card and '真实验证' in card


def test_real_limit_up_pool_today_survey_passes(tmp_path, monkeypatch, capsys):
    fixture = json.loads((Path(__file__).resolve().parents[1] / 'tests/fixtures/c1203/survey-results.json').read_text())
    monkeypatch.setattr('examples.pm.migration_survey', lambda repo, name: tuple(
        fixture['results'][name][key] for key in ('kind', 'writers', 'readers')))
    cards = tmp_path / 'cards'
    cards.mkdir()
    monkeypatch.setenv('PM_CARDS_DIR', str(cards))
    args = ['newcard', '--recipe', 'builder-fix', '--title', '迁移', '--serves', '目标',
            '--plan', 'P0 流程工具修复', '--approval', '已授权', '--scope', 'examples/pm.py',
            '--real-data', '真实数据', '--oracle', '验证', '--accept-by', 'fail-first-real-fixture',
            '--template', 'field-unify', '--function', 'limit_up_pool_today', '--tests', 'tests/test_pm.py', '--dry-run']
    assert main(args) == 0
    out = capsys.readouterr().out
    assert 'kind=point_snapshot' in out and '写端' in out and '读端' in out and '真实验证' in out
