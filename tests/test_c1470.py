from pathlib import Path

import pytest

from examples import pm


@pytest.mark.parametrize('recipe', ['builder-fix', 'builder-framework'])
@pytest.mark.parametrize('no_pack', [False, True])
def test_builder_card_permissions_and_context(recipe, no_pack, monkeypatch, capsys):
    monkeypatch.setenv('PM_CARDS_DIR', '/home/exedev/fw/cards')
    args = ['newcard', '--recipe', recipe, '--title', '修复', '--serves', '目标',
            '--plan', 'P0 §14', '--approval', '已授权', '--scope', 'examples/pm.py',
            '--goal', '修复问题', '--real-data', '真实卡', '--oracle', '结果一致',
            '--accept-by', 'fail-first-real-fixture', '--dry-run']
    if no_pack:
        args.append('--no-pack')
    assert pm.main(args) == 0
    card = capsys.readouterr().out
    assert pm.field(card, 'COMMIT') == 'no'
    assert ('先读 CONTEXT 文件' in card) is not no_pack
    assert '不联网' not in card and '不写 Redis/TD' not in card
    assert 'PERMISSIONS.md' in card and 'SCOPE_WRITE' in card
    assert '不 push' in card and '不部署' in card


def test_common_preserves_boundaries_and_authorized_framework_writes():
    common = Path('/home/exedev/fw/roles/COMMON.md').read_text(encoding='utf-8')
    assert '改 /home/exedev/fw' not in common
    for term in ('SCOPE_WRITE', '报告', '证据', 'PERMISSIONS.md', '凭据', 'legacy', '删除', '重启', '发信', 'push', '临时文件'):
        assert term in common
    assert '用户明确要求' in common and '卡' in common
