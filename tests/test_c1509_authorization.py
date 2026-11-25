"""Authorization text is checked without contacting a data source."""
import pytest
from examples import pm


ARGS = ['newcard', '--recipe', 'runner-verify', '--title', '验证', '--serves', '目标', '--plan', 'P0 14 T2', '--approval', '已授权', '--scope', 'examples/pm.py', '--goal', '验证', '--real-data', '真实卡', '--oracle', '一致', '--accept-by', 'real-run', '--question', '验证', '--dry-run']


@pytest.mark.parametrize('authorization', ['只读 Redis', '禁止写 TD', '不触发事件', '允许联网读取'])
def test_read_and_denial_do_not_grant_writes(authorization, capsys):
    assert pm.main(ARGS + ['--authorize', authorization]) == 0
    output = capsys.readouterr().out
    assert '不写 Redis/TD' in output
    assert '不触发事件' in output
    assert '【PM 明确允许】' not in output


def test_explicit_target_is_narrow(capsys):
    assert pm.main(ARGS + ['--authorize', '允许写 Redis 目标 core:test:c1509:']) == 0
    output = capsys.readouterr().out
    assert 'core:test:c1509:' in output
    assert '仅限' in output and '其他 Redis 目标' in output
    assert '不写 TD' in output and '不触发事件' in output


@pytest.mark.parametrize('authorization', ['允许写 Redis', '允许写 Redis/TD 并联网、删键、触发事件', '允许删键', '允许触发事件'])
def test_ambiguous_or_untargeted_grant_is_rejected(authorization, capsys):
    assert pm.main(ARGS + ['--authorize', authorization]) == 2
    assert 'REFUSED' in capsys.readouterr().out


def test_explicit_redis_prefix_is_not_arbitrarily_rejected(capsys):
    assert pm.main(ARGS + ['--authorize', '允许写 Redis 目标 core:test:']) == 0
    output = capsys.readouterr().out
    assert '仅限写 Redis 目标 core:test:' in output
    assert '不写 TD' in output and '不触发事件' in output


def test_td_target_is_narrow(capsys):
    assert pm.main(ARGS + ['--authorize', '允许写 TD 目标 core_data.c1509']) == 0
    output = capsys.readouterr().out
    assert '仅限写 TD 目标 core_data.c1509' in output
    assert '不写 Redis' in output and '不写其他 TD 目标' in output


@pytest.mark.parametrize('authorization', ['允许写 Redis 目标 q2:abc', '允许写 TD 目标 market_data1.table', '只读 Redis，允许写 TD', '不删键但允许触发事件'])
def test_protected_or_mixed_targets_are_rejected(authorization, capsys):
    assert pm.main(ARGS + ['--authorize', authorization]) == 2
    assert 'REFUSED' in capsys.readouterr().out


def test_runner_templates_allow_network_reads():
    import json
    from pathlib import Path
    recipes = Path(pm.__file__).with_name('card_recipes')
    for name in ('runner-observe', 'runner-verify', 'runner-realgate'):
        fixed = json.loads((recipes / (name + '.json')).read_text())['fixed']
        assert '不联网' not in fixed['AUTHORIZATION']
        assert '所有数据源网络读取默认可用' in fixed['AUTHORIZATION']
        assert '不写 Redis/TD' in fixed['AUTHORIZATION']
