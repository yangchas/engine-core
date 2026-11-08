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
