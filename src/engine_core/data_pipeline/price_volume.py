"""Incremental first-batch price indicators over adjusted daily bars.

The caller supplies trading-calendar continuity: an unknown missing trading day
must reset the checkpoint before the next bar.
"""
from __future__ import annotations

import math

from .adjust_factor import factor_for


_FIELDS = ('ma5', 'ma10', 'ma20', 'ma60', 'ema12', 'ema26', 'dif', 'dea',
           'macd', 'k', 'd', 'j', 'boll_upper', 'boll_middle', 'boll_lower',
           'boll_bandwidth')


def calculate_bar(bar, events, state=None):
    """Return (indicators, JSON-serializable checkpoint) for one observed bar."""
    state = dict(state or {})
    result = {name: None for name in _FIELDS}
    result['adjusted_close'] = None
    factor, quality = factor_for(events, str(bar['ts'])[:10])
    result['factor_quality'] = quality
    if bar.get('suspended') or factor is None:
        result['quality'] = 'PARTIAL'
        return result, {}

    def adjusted(name):
        value = bar.get(name)
        if value is None:
            return None
        value = float(value) * factor
        return value if math.isfinite(value) else None

    close, high, low = (adjusted(name) for name in ('close', 'high', 'low'))
    result['adjusted_close'] = close
    window = list(state.get('window', []))
    window.append([close, high, low])
    window = window[-60:]
    state['window'] = window
    if close is None:
        for key in ('ema12', 'ema26', 'dea'):
            state.pop(key, None)
    else:
        for period in (12, 26):
            key = f'ema{period}'
            previous = state.get(key)
            state[key] = close if previous is None else (2 / (period + 1) * close + (1 - 2 / (period + 1)) * previous)
            result[key] = state[key]
        result['dif'] = state['ema12'] - state['ema26']
        previous = state.get('dea')
        state['dea'] = result['dif'] if previous is None else 0.2 * result['dif'] + 0.8 * previous
        result['dea'] = state['dea']
        result['macd'] = 2 * (result['dif'] - state['dea'])
    for period in (5, 10, 20, 60):
        values = [row[0] for row in window[-period:]]
        if len(values) == period and all(v is not None for v in values):
            result[f'ma{period}'] = sum(values) / period
    if result['ma20'] is not None:
        mean = result['ma20']
        deviation = math.sqrt(sum((row[0] - mean) ** 2 for row in window[-20:]) / 20)
        result['boll_middle'] = mean
        result['boll_upper'] = mean + 2 * deviation
        result['boll_lower'] = mean - 2 * deviation
        if mean != 0:
            result['boll_bandwidth'] = (result['boll_upper'] - result['boll_lower']) / mean
    recent = window[-9:]
    if len(recent) == 9 and all(all(v is not None for v in row) for row in recent):
        highest = max(row[1] for row in recent)
        lowest = min(row[2] for row in recent)
        if highest != lowest:
            rsv = 100 * (close - lowest) / (highest - lowest)
            state['k'] = (2 * state.get('k', 50) + rsv) / 3
            state['d'] = (2 * state.get('d', 50) + state['k']) / 3
            result.update(k=state['k'], d=state['d'], j=3 * state['k'] - 2 * state['d'])
    if result['k'] is None:
        state.pop('k', None)
        state.pop('d', None)
    result['quality'] = 'FORMAL' if all(result[name] is not None for name in _FIELDS) else 'PARTIAL'
    return result, state
