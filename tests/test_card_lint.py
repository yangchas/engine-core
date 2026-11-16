from pathlib import Path

from examples.card_lint import lint_card

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/card_lint'


def findings(card):
    return lint_card((FIXTURES / f'{card}.md').read_text(), ROOT)


def test_historical_fixture_and_size_oracles():
    for card in ('C1031', 'C1039'):
        assert any('fixture_tool_not_in_scope' in line for line in findings(card))
    assert any('card_too_large' in line for line in findings('C1040'))


def test_historical_constraints_and_compatibility():
    result = findings('C1047')
    assert any('constraint_td_stops_0945' in line for line in result)
    assert any('constraint_t1v2_restart_zeroes_anchors' in line for line in result)
    assert not any(line.startswith('FAIL ') for line in findings('C1046'))
    assert not any('missing_path' in line for line in findings('C1028'))


def test_new_card_requires_fields():
    # Modified copy of real C1046: a new ID and no REAL_DATA field.
    card = (FIXTURES / 'C1046.md').read_text().replace('ID: C1046', 'ID: C1099', 1)
    card = '\n'.join(line for line in card.splitlines() if not line.startswith('REAL_DATA:'))
    assert any(line.startswith('FAIL required_fields:') for line in lint_card(card, ROOT))


def test_c1056_empty_scope_does_not_swallow_real_data():
    import re
    card = (FIXTURES / 'C1056.md').read_text()
    card = re.sub(r'^SCOPE_WRITE:.*$', 'SCOPE_WRITE:', card, flags=re.M)
    assert not any('required_fields' in line for line in lint_card(card, ROOT))
    missing = re.sub(r'^REAL_DATA:.*$', 'REAL_DATA:', card, flags=re.M)
    assert any('required_fields' in line and 'REAL_DATA' in line for line in lint_card(missing, ROOT))


def test_builder_recipe_defaults_on_historical_card_headers():
    import json
    import re

    for card_name in ('C1072', 'C1074'):
        original = (FIXTURES / f'card_{card_name}.md').read_text()
        assert not any('fixture_tool_not_in_scope' in line for line in lint_card(original, ROOT))
        previous = re.sub(r'^STUBS_ALLOWED:.*$', 'STUBS_ALLOWED: 仅回放真实响应', original, flags=re.M)
        assert any('fixture_tool_not_in_scope' in line for line in lint_card(previous, ROOT))
        for recipe in ('builder-fix', 'builder-framework'):
            defaults = json.loads((ROOT / 'examples/card_recipes' / f'{recipe}.json').read_text())['fixed']
            generated = re.sub(r'^STUBS_ALLOWED:.*$', lambda _: 'STUBS_ALLOWED: ' + defaults['STUBS_ALLOWED'], original, flags=re.M)
            assert not any('fixture_tool_not_in_scope' in line for line in lint_card(generated, ROOT)), (card_name, recipe)


def test_historical_capture_required_still_warns():
    import json

    card = (FIXTURES / 'C1031.md').read_text()
    assert any('fixture_tool_not_in_scope' in line for line in lint_card(card, ROOT))
    for recipe in ('builder-fix', 'builder-framework'):
        default = json.loads((ROOT / 'examples/card_recipes' / f'{recipe}.json').read_text())['fixed']['STUBS_ALLOWED']
        generated = card + f'\nSTUBS_ALLOWED: {default}\n'
        assert any('fixture_tool_not_in_scope' in line for line in lint_card(generated, ROOT)), recipe


def test_c1273_historical_scope_and_stubs_conflicts():
    # C1234: original SCOPE_WRITE; C1269: two original incorrect paths.
    scope = '/home/exedev/repos/engine_core-wt-C1234/src/engine_core/data_pipeline/daily_dde.py src/engine_core/data_pipeline/dde_derived.py tests/test_daily_dde_streaming.py tests/test_dde_derived.py'
    wrong = '/home/exedev/repos/engine_core-wt-C1269/src/engine_core/data_pipeline/limit_pool_d1.py, /home/exedev/repos/engine_core-wt-C1269/src/engine_core/data_pipeline/limit_up_today_sources.py'
    default = '无需新增 kind,不使用采集工具;夹具为真实文件摘录并附 PROVENANCE'
    goal = '测试:先写修复前失败的测试,夹具用真实 tick 与 0925 行与静态快照形状(经 capture_real_fixture 录制并登记 PROVENANCE)'
    def card(paths, stubs, objective):
        return f'SCOPE_WRITE: {paths}\nSTUBS_ALLOWED: {stubs}\nGOAL: {objective}\n'
    for paths in (scope, wrong):
        assert any(x.startswith('FAIL scope_path:') and 'find' in x for x in lint_card(card(paths, default, '修复'), ROOT))
    assert any(x.startswith('FAIL stubs_conflict:') for x in lint_card(card('examples/pm.py', default, goal), ROOT))
    good = card('src/engine_core/guardian/limit_pool_d1.py, examples/pm.py', '允许使用采集工具录制真实夹具', goal)
    assert not any(x.startswith(('FAIL scope_path:', 'FAIL stubs_conflict:')) for x in lint_card(good, ROOT))
    new = card('examples/new_c1273_example.py', default, '新建 new_c1273_example.py')
    assert not any(x.startswith('FAIL scope_path:') for x in lint_card(new, ROOT))
