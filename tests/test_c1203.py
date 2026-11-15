import json
from pathlib import Path

from examples import pm


def test_registry_excerpt_and_real_survey(tmp_path, monkeypatch, capsys):
    repo = Path(__file__).resolve().parents[1]
    excerpt = json.loads((repo / 'tests/fixtures/c1203/registry-excerpt.json').read_text())
    actual = json.loads((repo / 'src/engine_core/guardian/datasets.registry.json').read_text())
    assert excerpt['items'] == [item for item in actual['items'] if item['id'] in ('hot_rank', 'limit_up_pool_today')]
    monkeypatch.setenv('PM_CARDS_DIR', str(tmp_path))
    common = ['newcard', '--recipe', 'builder-fix', '--title', '迁移', '--serves', '目标',
              '--plan', 'P0 流程工具修复', '--approval', '已授权', '--scope', 'examples/pm.py',
              '--real-data', '真实数据', '--oracle', '验证', '--accept-by', 'fail-first-real-fixture',
              '--template', 'field-unify', '--tests', 'tests/test_pm.py', '--dry-run']
    assert pm.main([*common, '--function', 'hot_rank']) == 2
    assert 'kind=unknown' in capsys.readouterr().out
    assert pm.main([*common, '--function', 'limit_up_pool_today']) == 0
    output = capsys.readouterr().out
    assert 'kind=point_snapshot' in output and 'warning' in output.lower()
    kind, writers, readers = pm.migration_survey(repo, 'limit_up_pool_today')
    assert len(readers) > 6 and 'src/engine_core/guardian/preopen_check.py' not in writers
    monkeypatch.setattr(pm, 'migration_survey', lambda repo, name: (kind, readers[:3], readers))
    assert pm.main([*common, '--function', 'limit_up_pool_today']) == 2
    assert 'REFUSED: survey report required' in capsys.readouterr().out
