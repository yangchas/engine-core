import json
from pathlib import Path

import pytest

from engine_core.data_pipeline.chip_peak import calculate_windows

PACK = Path(__file__).parent / 'fixtures' / 'chip_peak_c879'


def test_state_insert_byte_limit():
    import re
    from engine_core.data_pipeline.chip_peak import ChipPeakStore, prepare_window
    root = Path(__file__).parent / 'fixtures' / 'price_volume_c852'
    bars = [json.loads(line) for line in (root / 'daily_kline.jsonl').read_text().splitlines()]
    bars = [b for b in bars if b['symbol'] == '600519' and not b['suspended']][:120]
    events = json.loads((root / 'factor_events.json').read_text())['600519']
    state = None
    for bar in bars:
        _, state = prepare_window(bar, events, state)
    # Size expansion of the existing C852 capture, not new upstream observations.
    states = {f'{n:06d}': state for n in range(600)}
    class Recorder:
        database = 'core_data'
        def __init__(self): self.sql = []
        def _run(self, sql): self.sql.append(sql)
    recorder = Recorder()
    ChipPeakStore(recorder).write_states(states)
    assert all(len(sql.encode('utf-8')) <= 512 * 1024 for sql in recorder.sql)
    symbols = [s for sql in recorder.sql for s in re.findall(r'chip_window_state_(\d{6}) USING', sql)]
    assert symbols == sorted(states)


def test_state_capacity_error():
    from engine_core.data_pipeline.chip_peak import ChipPeakStore, STATE_JSON_CAPACITY
    class Recorder:
        database = 'core_data'
        def _run(self, sql): pytest.fail('oversized state must not be written')
    with pytest.raises(ValueError, match='chip_window_state_json_exceeds_capacity'):
        ChipPeakStore(Recorder()).write_states({'600519': {'window': 'x' * STATE_JSON_CAPACITY}})


def test_result_insert_byte_limit():
    import re
    from engine_core.data_pipeline.chip_peak import ChipPeakStore
    from engine_core.data_pipeline.td_sink import INSERT_MAX_SQL_BYTES
    row = json.loads((PACK / 'expected_chip.jsonl').read_text().splitlines()[0])
    # Expand an existing captured reference only to exercise future wider SQL values.
    row['input_refs'] = json.dumps(row, ensure_ascii=False) * 8
    rows = {f'{n:06d}': row for n in range(600)}
    class Recorder:
        database = 'core_data'
        def __init__(self): self.sql = []
        def _run(self, sql): self.sql.append(sql)
    recorder = Recorder()
    ChipPeakStore(recorder).write_results('2026-11-06', rows)
    assert all(len(sql.encode('utf-8')) <= INSERT_MAX_SQL_BYTES for sql in recorder.sql)
    assert [s for sql in recorder.sql for s in re.findall(r'chip_indicators_(\d{6}) USING', sql)] == sorted(rows)


def test_price_volume_state_insert_byte_limit():
    import re
    from engine_core.data_pipeline.price_volume import PriceVolumeStore
    from engine_core.data_pipeline.td_sink import INSERT_MAX_SQL_BYTES
    root = Path(__file__).parent / 'fixtures' / 'price_volume_c852'
    bars = [json.loads(line) for line in (root / 'daily_kline.jsonl').read_text().splitlines()]
    # Reuse real captured bars as size expansion; these are not new observations.
    state = {'window': [b for b in bars if b['symbol'] == '600519'][:120]}
    states = {f'{n:06d}': state for n in range(100)}
    class Recorder:
        database = 'core_data'
        def __init__(self): self.sql = []
        def _run(self, sql): self.sql.append(sql)
    recorder = Recorder()
    PriceVolumeStore(recorder).write_states(states)
    assert all(len(sql.encode('utf-8')) <= INSERT_MAX_SQL_BYTES for sql in recorder.sql)
    assert [s for sql in recorder.sql for s in re.findall(r'price_volume_state_(\d{6}) USING', sql)] == sorted(states)


def test_incremental_empty_state_never_writes():
    from engine_core.data_pipeline.chip_peak import run
    root = Path(__file__).parent / 'fixtures' / 'price_volume_c852'
    bar = json.loads((root / 'daily_kline.jsonl').read_text().splitlines()[0])
    events = json.loads((root / 'factor_events.json').read_text())[bar['symbol']]
    class Store:
        def __init__(self): self.writes = []
        def ensure_schema(self): pass
        def read_day_rows(self, day): return {bar['symbol']: bar}
        def read_events_many(self, symbols): return {bar['symbol']: (events, 'FORMAL')}
        def read_states(self, symbols): return {}
        def write_results(self, *args): self.writes.append('results')
        def write_states(self, *args): self.writes.append('states')
        def write_day_marker(self, *args): self.writes.append('marker')
        def count_day_results(self, day): return 1
    store = Store()
    code, reply = run({'trade_date': bar['ts'][:10]}, '.', store=store)
    assert code == 10 and reply['detail']['hint'] == 'NEEDS_REBUILD'
    assert reply['reason'] == 'NEEDS_REBUILD'
    assert store.writes == []


def test_reference_boundaries():
    cases = [json.loads(line) for line in (PACK / 'boundary_cases.jsonl').read_text().splitlines()]
    for case in cases:
        actual = calculate_windows([[row['close'] for row in case['input']]],
                                   [[row['turn'] for row in case['input']]])[0]
        for key, expected in case['spec'].items():
            if isinstance(expected, (int, float)) and not isinstance(expected, bool):
                assert actual[key] == pytest.approx(expected, rel=0, abs=1e-9), (case['case'], key)
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
                assert out[key] == pytest.approx(value, rel=0, abs=1e-9), (symbol, day, key, out[key], value)
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


def test_schema_and_state_capacity():
    import re
    from engine_core.data_pipeline.chip_peak import ChipPeakStore, STATE_JSON_CAPACITY
    class Recorder:
        database = 'core_data'
        def __init__(self): self.sql = []
        def _run(self, sql, **kwargs):
            self.sql.append(sql)
            return []
    recorder = Recorder()
    store = ChipPeakStore(recorder)
    store.ensure_schema()
    assert len(recorder.sql) == 3
    for sql in recorder.sql:
        columns, _, tags = sql.partition(' TAGS ')
        widths = re.findall(r'\b(VARCHAR|NCHAR|BINARY)\s*\(\s*(\d+)\s*\)', columns, re.I)
        tag_widths = re.findall(r'\b(VARCHAR|NCHAR|BINARY)\s*\(\s*(\d+)\s*\)', tags, re.I)
        assert widths and all(0 < int(n) <= 60000 for _, n in widths + tag_widths)
        assert sum(int(n) * (4 if kind.upper() == 'NCHAR' else 1) for kind, n in widths) <= 60100
    root = Path(__file__).parent / 'fixtures' / 'price_volume_c852'
    bars = [json.loads(line) for line in (root / 'daily_kline.jsonl').read_text().splitlines()]
    bars = [b for b in bars if b['symbol'] == '600519' and not b['suspended']][:120]
    state = {'window': [[b['ts'][:10], b['close'], b['turnover_pct']] for b in bars], 'last_trade_date': bars[-1]['ts'][:10]}
    assert len(json.dumps(state).encode()) < STATE_JSON_CAPACITY
    store.write_states({'600519': state})
    store.write_states({'600519': state})
    assert recorder.sql[-1] == recorder.sql[-2]


def test_incremental_replay_preserves_state_and_marker():
    from engine_core.data_pipeline.chip_peak import run, prepare_window
    root = Path(__file__).parent / 'fixtures' / 'price_volume_c852'
    bars = [json.loads(line) for line in (root / 'daily_kline.jsonl').read_text().splitlines()]
    bars = [b for b in bars if b['symbol'] == '600519' and not b['suspended']]
    events = json.loads((root / 'factor_events.json').read_text())['600519']
    state = None
    for bar in bars[:10]:
        _, state = prepare_window(bar, events, state)
    state['last_trade_date'] = bars[9]['ts'][:10]
    class Store:
        def __init__(self): self.states = {'600519': state}; self.rows = {}; self.markers = {}
        def ensure_schema(self): pass
        def read_day_rows(self, day): return {'600519': bars[10]}
        def read_states(self, symbols): return self.states.copy()
        def read_events_many(self, symbols): return {'600519': (events, 'FORMAL')}
        def write_results(self, day, rows): self.rows.update(rows)
        def write_states(self, states): self.states.update(states)
        def read_day_marker(self, day): return self.markers.get(day)
        def count_day_results(self, day): return len(self.rows)
        def write_day_marker(self, *args): self.markers[args[0]] = args
    store = Store()
    day = bars[10]['ts'][:10]
    code, reply = run({'trade_date': day}, '.', store=store)
    assert code == 0 and reply['detail']['computed'] == 1
    saved = json.dumps([store.states, store.rows, store.markers], sort_keys=True)
    code, reply = run({'trade_date': day}, '.', store=store)
    assert code == 0 and reply['detail']['skipped_already_processed'] == 1
    assert json.dumps([store.states, store.rows, store.markers], sort_keys=True) == saved


def test_rebuild_and_incremental_match_real_windows():
    from engine_core.data_pipeline.chip_peak import FIELDS, run
    root = Path(__file__).parent / 'fixtures' / 'price_volume_c852'
    bars = [json.loads(line) for line in (root / 'daily_kline.jsonl').read_text().splitlines()]
    bars = [b for b in bars if b['symbol'] == '600519' and not b['suspended']][:12]
    events = json.loads((root / 'factor_events.json').read_text())['600519']
    days = [b['ts'][:10] for b in bars]
    class Store:
        def __init__(self): self.states = {}; self.rows = {}; self.markers = {}
        def ensure_schema(self): pass
        def trading_dates(self, day): return days
        def read_day_rows(self, day): return {'600519': bars[days.index(day)]}
        def read_states(self, symbols): return self.states.copy()
        def read_events_many(self, symbols): return {'600519': (events, 'FORMAL')}
        def write_results(self, day, rows): self.rows.setdefault(day, {}).update(rows)
        def write_states(self, states): self.states.update(states)
        def count_day_results(self, day): return len(self.rows.get(day, {}))
        def write_day_marker(self, *args): self.markers[args[0]] = args
    full, replay = Store(), Store()
    assert run({'trade_date': days[-1], 'args': {'mode': 'rebuild'}}, '.', store=full)[0] == 0
    assert run({'trade_date': days[-3], 'args': {'mode': 'rebuild'}}, '.', store=replay)[0] == 0
    for day in days[-2:]:
        assert run({'trade_date': day}, '.', store=replay)[0] == 0
    assert replay.states == full.states
    for day in days:
        a, b = full.rows[day]['600519'], replay.rows[day]['600519']
        assert {k: a[k] for k in FIELDS + ('quality', 'warm', 'bars_used', 'input_refs')} == {k: b[k] for k in FIELDS + ('quality', 'warm', 'bars_used', 'input_refs')}
    saved = json.dumps([replay.states, replay.rows, replay.markers], sort_keys=True)
    assert run({'trade_date': days[-3]}, '.', store=replay)[1]['detail']['skipped_already_processed'] == 1
    assert json.dumps([replay.states, replay.rows, replay.markers], sort_keys=True) == saved


def test_cli_dry_run_without_services():
    import os
    import subprocess
    import sys
    env = dict(os.environ, GUARDIAN_DRY_RUN='1')
    env.pop('TD_CONFIG', None)
    env.pop('REDIS_URL', None)
    script = Path(__file__).parents[1] / 'examples' / 'build_chip_peak.py'
    result = subprocess.run([sys.executable, str(script), '--date', '2026-11-06', '--mode', 'rebuild',
                             '--from-date', '2026-01-01'], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'ok'
