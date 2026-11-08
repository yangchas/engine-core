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


def test_factor_quality_and_missing_bar_preserve_state():
    bars = [json.loads(x) for x in (FIXTURES / 'daily_kline.jsonl').read_text().splitlines() if json.loads(x)['symbol'] == '600519']
    events = json.loads((FIXTURES / 'factor_events.json').read_text())['600519']
    _, state = calculate_bar(bars[0], events)
    result, unchanged = calculate_bar(bars[1], None, state)
    assert result is None and unchanged == state
    result, unchanged = calculate_bar(None, events, state)
    assert result is None and unchanged == state
    result, _ = calculate_bar(bars[1], [], state)
    assert result['quality'] == 'PARTIAL' and 'FACTOR_NO_EVENTS' in result['partial_reasons']
    result, _ = calculate_bar(bars[0], [max(events, key=lambda e: e['dividOperateDate'])])
    assert 'FACTOR_BEFORE_FIRST_EVENT' in result['partial_reasons']


def test_controlled_gap_resets_to_post_gap_real_bars():
    # Real upstream rows, with three consecutive rows deliberately removed.
    bars = [json.loads(x) for x in (FIXTURES / 'daily_kline.jsonl').read_text().splitlines() if json.loads(x)['symbol'] == '600519']
    events = json.loads((FIXTURES / 'factor_events.json').read_text())['600519']
    state = None
    for bar in bars[:30]:
        _, state = calculate_bar(bar, events, state)
    after = fresh = None
    for i, bar in enumerate(bars[33:55]):
        result, after = calculate_bar(bar, events, state if i == 0 else after, gap_before=i == 0)
        expected, fresh = calculate_bar(bar, events, fresh)
        assert result == expected


def test_real_reference_all_symbols_and_new_listing():
    bars = [json.loads(x) for x in (FIXTURES / 'daily_kline.jsonl').read_text().splitlines()]
    expected = [json.loads(x) for x in (FIXTURES / 'expected.jsonl').read_text().splitlines()]
    events = json.loads((FIXTURES / 'factor_events.json').read_text())
    states, previous = {}, {}
    days = sorted({str(row['ts'])[:10] for row in bars if row['symbol'] == '600519'})
    positions = {day: i for i, day in enumerate(days)}
    for bar, reference in zip(bars, expected, strict=True):
        symbol, day = bar['symbol'], str(bar['ts'])[:10]
        old = states.get(symbol)
        gap = symbol in previous and positions[day] != positions[previous[symbol]] + 1
        result, states[symbol] = calculate_bar(bar, events[symbol], old, gap_before=gap)
        if result is None:
            assert bar['suspended']
            assert states[symbol] == (old or {})
            continue
        previous[symbol] = day
        if symbol == '001381':
            assert result['ma5'] is None and result['quality'] == 'PARTIAL'
        for name in ('adjusted_close', 'ema12', 'ema26', 'dif', 'dea', 'macd',
                     'ma5', 'ma10', 'ma20', 'ma60', 'boll_middle', 'boll_upper',
                     'boll_lower', 'boll_bandwidth', 'k', 'd', 'j'):
            value = reference[name]
            if value is None:
                assert result[name] is None, (symbol, day, name)
            else:
                assert result[name] == pytest.approx(value, rel=1e-9, abs=1e-12), (symbol, day, name)


def test_state_snapshot_capacity_and_fixed_timestamp():
    from engine_core.data_pipeline.price_volume import PriceVolumeStore, STATE_JSON_CAPACITY

    class RecordingStore:
        database = 'core_data'
        def __init__(self):
            self.sql = []
        def _run(self, sql, **kwargs):
            self.sql.append(sql)
            return []

    bars = [json.loads(x) for x in (FIXTURES / 'daily_kline.jsonl').read_text().splitlines()
            if json.loads(x)['symbol'] == '600519']
    events = json.loads((FIXTURES / 'factor_events.json').read_text())['600519']
    state = None
    for bar in bars[:60]:
        _, state = calculate_bar(bar, events, state)
    assert len(json.dumps(state).encode()) < STATE_JSON_CAPACITY
    recorder = RecordingStore()
    store = PriceVolumeStore(recorder)
    store.write_states({'600519': state})
    store.write_states({'600519': state})
    assert recorder.sql[0] == recorder.sql[1]
    assert 'price_volume_state_600519' in recorder.sql[0]



def test_run_incremental_real_bar_matches_full_calculation():
    from engine_core.data_pipeline.price_volume import run

    bars = [json.loads(x) for x in (FIXTURES / 'daily_kline.jsonl').read_text().splitlines()
            if json.loads(x)['symbol'] == '600519']
    events = json.loads((FIXTURES / 'factor_events.json').read_text())['600519']
    state = None
    for bar in bars[:-1]:
        _, state = calculate_bar(bar, events, state)
    expected, _ = calculate_bar(bars[-1], events, state)

    class Store:
        def ensure_schema(self): pass
        def read_day_rows(self, day): return {'600519': bars[-1]}
        def read_states(self, symbols): return {'600519': state}
        def read_events_many(self, symbols): return {'600519': (events, 'FORMAL')}
        def write_results(self, day, rows): self.rows = rows
        def write_states(self, states): self.states = states
        def write_day_marker(self, *args): self.marker = args

    store = Store()
    code, reply = run({'trade_date': str(bars[-1]['ts'])[:10]}, '.', store=store)
    assert code == 0 and reply['status'] == 'ok'
    for name in ('ma5', 'ema12', 'macd', 'k', 'boll_upper'):
        assert store.rows['600519'][name] == pytest.approx(expected[name])
    assert store.marker[3] == 1


def test_run_resets_only_for_missing_not_suspended_real_bars():
    from datetime import date
    from engine_core.data_pipeline.price_volume import run

    bars = [json.loads(x) for x in (FIXTURES / 'daily_kline.jsonl').read_text().splitlines()
            if json.loads(x)['symbol'] == '600519']
    events = json.loads((FIXTURES / 'factor_events.json').read_text())['600519']
    old = None
    for bar in bars[:30]:
        _, old = calculate_bar(bar, events, old)
    old['last_trade_date'] = str(bars[29]['ts'])[:10]
    days = [str(b['ts'])[:10] for b in bars[29:34]]
    expected, _ = calculate_bar(bars[33], events)

    class Store:
        def ensure_schema(self): pass
        def previous_day(self, day): return date.fromisoformat(days[days.index(day) - 1])
        def read_day_rows(self, day):
            return {'600519': bars[33]} if day == days[-1] else {}
        def read_states(self, symbols): return {'600519': old}
        def read_events_many(self, symbols): return {'600519': (events, 'FORMAL')}
        def write_results(self, day, rows): self.rows = rows
        def write_states(self, states): pass
        def write_day_marker(self, *args): pass
    store = Store()
    code, _ = run({'trade_date': days[-1]}, '.', store=store)
    assert code == 0
    assert store.rows['600519']['ema12'] == expected['ema12']
    assert store.rows['600519']['ma5'] is None
