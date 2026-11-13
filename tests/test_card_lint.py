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
