from pathlib import Path

import pytest

from examples import pm


@pytest.mark.parametrize('recipe', ['builder-fix', 'builder-framework'])
def test_builder_card_and_injected_role_follow_current_test_rules(recipe, monkeypatch, capsys):
    monkeypatch.setenv('PM_CARDS_DIR', '/home/exedev/fw/cards')
    args = ['newcard', '--recipe', recipe, '--title', '修复', '--serves', '目标',
            '--plan', 'P0 §14', '--approval', '已授权', '--scope', 'examples/pm.py',
            '--goal', '修复问题', '--real-data', '真实数据', '--oracle', '结果一致',
            '--accept-by', 'fail-first-real-fixture', '--dry-run']
    assert pm.main(args) == 0
    card = capsys.readouterr().out
    role = Path('/home/exedev/fw/roles/builder.md').read_text(encoding='utf-8')
    for text in (card, role):
        assert '新增测试 ≤' not in text
        assert 'MAX_NEW_TESTS' not in text
        assert '150' not in text and '90 秒' not in text
        assert 'run_function.py' not in text
        assert '只跑 TEST_BUDGET' not in text
    assert '按实际影响' in card
    assert '直接导入' in role and '真实数据' in role and '临时数据' in role
