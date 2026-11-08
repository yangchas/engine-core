import json
from pathlib import Path

import pytest

from engine_core.data_pipeline.chip_peak import calculate_windows

PACK = Path(__file__).parent / 'fixtures' / 'chip_peak_c879'


def test_reference_boundaries():
    cases = [json.loads(line) for line in (PACK / 'boundary_cases.jsonl').read_text().splitlines()]
    for case in cases:
        actual = calculate_windows([[row['close'] for row in case['input']]],
                                   [[row['turn'] for row in case['input']]])[0]
        for key, expected in case['spec'].items():
            if isinstance(expected, (int, float)) and not isinstance(expected, bool):
                assert actual[key] == pytest.approx(expected, abs=1e-9), (case['case'], key)
            else:
                assert actual[key] == expected, (case['case'], key)


def test_all_c879_reference_values():
    from collections import defaultdict
    from decimal import Decimal
    from engine_core.data_pipeline.adjust_factor import factor_for
    root = Path(__file__).parent / 'fixtures' / 'price_volume_c852'
    bars = [json.loads(line) for line in (root / 'daily_kline.jsonl').read_text().splitlines()]
    events = json.loads((root / 'factor_events.json').read_text())
    references = [json.loads(line) for line in (PACK / 'expected_chip.jsonl').read_text().splitlines()]
    histories = defaultdict(list)
    for bar in bars:
        if not bar['suspended']:
            histories[bar['symbol']].append(bar)
    comparisons = 0
    for reference in references:
        symbol, day = reference['symbol'], reference['date']
        window = [b for b in histories[symbol] if b['ts'][:10] <= day][-120:]
        prices = [float(Decimal(str(b['close'])) * Decimal(str(factor_for(events[symbol], b['ts'][:10])[0]))) for b in window]
        out = calculate_windows([prices], [[b['turnover_pct'] for b in window]])[0]
        for key in out:
            value = reference[key]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                assert out[key] == pytest.approx(value, abs=1e-9), (symbol, day, key, out[key], value)
            else:
                assert out[key] == value, (symbol, day, key)
        comparisons += 1
    assert comparisons == 823


def test_window_state_and_factor_quality():
    from engine_core.data_pipeline.chip_peak import prepare_window
    root = Path(__file__).parent / 'fixtures' / 'price_volume_c852'
    bars = [json.loads(line) for line in (root / 'daily_kline.jsonl').read_text().splitlines()]
    bars = [bar for bar in bars if bar['symbol'] == '600519' and not bar['suspended']]
    events = json.loads((root / 'factor_events.json').read_text())['600519']
    state = None
    for bar in bars[:120]:
        prepared, state = prepare_window(bar, events, state)
    assert len(state['window']) == 120
    assert prepared['bars_used'] == 120 and prepared['factor_reasons'] == []
    previous = state.copy()
    assert prepare_window(None, events, state) == (None, previous)
    suspended = dict(bars[-1], suspended=True)
    assert prepare_window(suspended, events, state) == (None, previous)
    assert prepare_window(bars[-1], events, state, factor_quality='UNPROCESSED') == (None, previous)
    prepared, newer = prepare_window(bars[-1], [], state)
    assert len(newer['window']) == 120 and prepared['factor_reasons'] == ['FACTOR_NO_EVENTS']
    assert newer['window'][0] == state['window'][1]
    future = [dict(events[-1], dividOperateDate='2099-01-01')]
    prepared, _ = prepare_window(bars[0], future)
    assert prepared['factor_reasons'] == ['FACTOR_BEFORE_FIRST_EVENT']
