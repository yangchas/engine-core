import json
from pathlib import Path

from examples import pm


def test_registry_excerpt_and_real_survey(tmp_path, monkeypatch, capsys):
    repo = Path(__file__).resolve().parents[1]
    excerpt = json.loads((repo / 'tests/fixtures/c1203/registry-excerpt.json').read_text())
    survey = json.loads((repo / 'tests/fixtures/c1203/survey-results.json').read_text())
    by_id = {item['id']: item for item in excerpt['items']}
    assert by_id['hot_rank'].get('kind') is None
    assert by_id['limit_up_pool_today']['kind'] == 'point_snapshot'
    monkeypatch.setenv('PM_CARDS_DIR', str(tmp_path))
    common = ['newcard', '--recipe', 'builder-fix', '--title', '迁移', '--serves', '目标',
              '--plan', 'P0 流程工具修复', '--approval', '已授权', '--scope', 'examples/pm.py',
              '--real-data', '真实数据', '--oracle', '验证', '--accept-by', 'fail-first-real-fixture',
              '--template', 'field-unify', '--tests', 'tests/test_pm.py', '--dry-run']
    monkeypatch.setattr(pm, 'migration_survey', lambda repo, name: tuple(
        survey['results'][name][key] for key in ('kind', 'writers', 'readers')))
    assert pm.main([*common, '--function', 'hot_rank']) == 2
    assert 'kind=unknown' in capsys.readouterr().out
    assert pm.main([*common, '--function', 'limit_up_pool_today']) == 0
    output = capsys.readouterr().out
    assert 'kind=point_snapshot' in output and 'warning' in output.lower()
    kind, writers, readers = (survey['results']['limit_up_pool_today'][key]
                              for key in ('kind', 'writers', 'readers'))
    assert len(readers) > 6 and 'src/engine_core/guardian/preopen_check.py' not in writers
    monkeypatch.setattr(pm, 'migration_survey', lambda repo, name: (kind, readers[:3], readers))
    assert pm.main([*common, '--function', 'limit_up_pool_today']) == 2
    assert 'REFUSED: survey report required' in capsys.readouterr().out
