from pathlib import Path
import shutil

from examples import pm


def setup_cards(tmp_path, monkeypatch):
    cards = tmp_path / 'cards'
    shutil.copytree(Path(__file__).parent / 'fixtures/pm_cards', cards)
    monkeypatch.setenv('PM_CARDS_DIR', str(cards))
    monkeypatch.setenv('PM_EVIDENCE_DIR', str(tmp_path / 'evidence'))
    monkeypatch.setenv('PM_NO_WORKTREE', '1')
    return cards


def args():
    return ['newcard', '--recipe', 'builder-fix', '--title', '修复', '--serves', '目标',
            '--plan', 'P0 2/6', '--approval', '已授权', '--scope', 'examples/pm.py',
            '--real-data', '真实卡', '--oracle', '结果一致', '--accept-by', 'fail-first-real-fixture', '--no-pack']


def test_refuse_nonready_without_touching_card(tmp_path, monkeypatch, capsys):
    cards = setup_cards(tmp_path, monkeypatch)
    card = cards / 'C1124.md'
    original = (Path(__file__).parent / 'fixtures/pm_ship/C1079-card.txt').read_text()
    original = pm.set_status(original, 'STATUS: PARTIAL(等待合并)')
    card.write_text(original)
    for command in ('run', 'dispatch', 'ship'):
        assert pm.main([command, 'C1124']) != 0
        assert capsys.readouterr().out == 'REFUSED C1124 status is PARTIAL(等待合并), not ready; not touched\n'
        assert card.read_text() == original


def test_newcard_run_and_failed_lint(tmp_path, monkeypatch, capsys):
    cards = setup_cards(tmp_path, monkeypatch)
    for goal in ('路径 /home/exedev/engine_core-wt-CARD/missing', '处理 <占位>'):
        before = set(cards.iterdir())
        assert pm.main(args() + ['--goal', goal]) != 0
        assert 'FAIL ' in capsys.readouterr().out
        assert set(cards.iterdir()) == before
    monkeypatch.setattr(pm, 'ship', lambda a: print('NOT MERGED stub') or 2)
    assert pm.main(args() + ['--goal', '修复问题', '--run']) == 2
    assert capsys.readouterr().out.splitlines() == ['CARD C1048', 'NOT MERGED stub']
    assert (cards / 'C1048.md').is_file()


def test_newcard_templates(tmp_path, monkeypatch, capsys):
    setup_cards(tmp_path, monkeypatch)
    for template, terms in [('field-unify', ('+08:00', '审计', '兼容', 'fail-first', '规范化')), ('catalog-retire', ('Redis-only', 'MISSING', 'hot_view_expire_at', '5交易日'))]:
        assert pm.main(args() + ['--template', template, '--function', 'hot_plates', '--tests', 'tests/test_pm.py', 'tests/test_release.py', '--goal', '额外要求', '--dry-run']) == 0
        text = capsys.readouterr().out
        assert all(term in text for term in (*terms, 'hot_plates', 'tests/test_pm.py', 'tests/test_release.py', '额外要求'))
