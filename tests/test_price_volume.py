import json
from pathlib import Path

import pytest

from engine_core.data_pipeline.price_volume import calculate_bar

FIXTURES = Path(__file__).parent / 'fixtures' / 'price_volume_c852'


def test_real_reference_first_symbol():
    bars = [json.loads(x) for x in (FIXTURES / 'daily_kline.jsonl').read_text().splitlines()]
    expected = [json.loads(x) for x in (FIXTURES / 'expected.jsonl').read_text().splitlines()]
    events = json.loads((FIXTURES / 'factor_events.json').read_text())
    state = None
    for bar, reference in zip(bars, expected, strict=True):
        if bar['symbol'] != '600519':
            continue
        result, state = calculate_bar(bar, events['600519'], state)
        for name in ('adjusted_close', 'ema12', 'ema26', 'dif', 'dea', 'macd',
                     'ma5', 'ma10', 'ma20', 'ma60', 'boll_middle', 'boll_upper',
                     'boll_lower', 'boll_bandwidth', 'k', 'd', 'j'):
            if reference[name] is None:
                assert result[name] is None, (bar['ts'], name)
            else:
                assert result[name] == pytest.approx(reference[name], rel=1e-9, abs=1e-12), (bar['ts'], name)
