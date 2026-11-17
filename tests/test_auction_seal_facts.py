import gzip
import json
from pathlib import Path

from engine_core.data_pipeline.auction_seal_facts import build_facts

REAL = Path(__file__).parent / 'fixtures' / 'real'


def test_real_auction_candidates_and_tick_seals():
    with gzip.open(REAL / 'c1256/auction.json.gz', 'rt') as f:
        auction = json.load(f)
    with gzip.open(REAL / 'core_artifacts/c594/stock_static/2026-10-29/c573_guardian/rows.jsonl.gz', 'rt') as f:
        listings = {row['symbol']: row['listing_date'] for line in f if (row := json.loads(line))}
    for symbol in ('001381', '688827'):
        raw = json.loads(next(REAL.glob(f'td_stock_static_2026-11-16_{symbol}_*.json')).read_text())
        listings[symbol] = json.loads(raw['rows'][0][raw['columns'].index('payload')])['listing_date']
    ticks = {}
    for path in REAL.glob('td_stock_tick_2026-11-16_*_092430_092600_*.json'):
        raw = json.loads(path.read_text())
        ticks[path.name.split('_')[4]] = [dict(zip(raw['columns'], row)) for row in raw['rows']]
    calendar = json.loads((REAL.parent / 'trade_calendar/baostock-2026-01-01_2026-12-31.json').read_text())
    dates = [row['calendar_date'] for row in calendar['rows'] if row['is_trading_day'] == '1']
    result = build_facts('2026-11-16', auction, listings, dates, lambda symbol: ticks[symbol])
    assert sum(row['side'] == 'limit_up' for row in result['rows']) == 7
    assert sum(row['side'] == 'limit_down' for row in result['rows']) == 4
    assert '688827' in result['unlimited'] and '001381' not in result['unlimited']
    by_symbol = {row['symbol']: row for row in result['rows']}
    assert by_symbol['002905']['price_source'] == 'indicative_bid_ask_mid'
    assert by_symbol['002973']['price_source'] == 'indicative_bid_ask_mid'
    assert by_symbol['000831']['price_source'] == 'auction_match'
    assert by_symbol['002905']['basis'] == 'pre_0924_57'
    assert by_symbol['002905']['seal_amount_yuan'] == 90844380
    assert by_symbol['002973']['basis'] == 'pre_0924_57'
    assert by_symbol['002973']['seal_amount_yuan'] == 1276612
    amounts = {'000819': 32898120, '000831': 1102004145, '002148': 511797125,
               '002242': 848119626, '002425': 488070, '002905': 90844380,
               '600880': 10649, '002973': 1276612, '300779': 229083876,
               '603381': 5456580, '603536': 56127}
    assert {symbol: row['seal_amount_yuan'] for symbol, row in by_symbol.items()} == amounts
    assert by_symbol['002242']['basis'] == 'final_0925'
    assert by_symbol['002242']['seal_volume'] == 28818200  # 09:24:57 matched buy-one is 38578 lots


def test_real_limit_price_before_auction_does_not_override_final_tick():
    raw = json.loads(next(REAL.glob('td_stock_tick_2026-11-16_002129_092457_092503_*.json')).read_text())
    ticks = [dict(zip(raw['columns'], row)) for row in raw['rows']]
    pre, final = ticks
    assert (pre['px_milli'], final['px_milli'], pre['bv1'], pre['av2']) == (10320, 10300, 176588, 55853)
    with gzip.open(REAL / 'core_artifacts/c594/stock_static/2026-10-29/c573_guardian/rows.jsonl.gz', 'rt') as f:
        listings = {row['symbol']: row['listing_date'] for line in f if (row := json.loads(line))}
    calendar = json.loads((REAL.parent / 'trade_calendar/baostock-2026-01-01_2026-12-31.json').read_text())
    dates = [row['calendar_date'] for row in calendar['rows'] if row['is_trading_day'] == '1']
    quote = {'002129': {'price_milli': pre['px_milli'], 'pre_close_milli': pre['pc_milli']}}
    result = build_facts('2026-11-16', quote, listings, dates, lambda symbol: ticks)
    row = result['rows'][0]
    assert row['price_source'] == 'missing'
    assert row['basis'] is None and row['seal_volume'] is None and row['seal_amount_yuan'] is None



def test_run_uses_latest_visible_real_stock_static_snapshot(monkeypatch):
    from datetime import datetime
    import pytest
    from engine_core.data_pipeline import auction_seal_facts, auction_redis_view, reference_snapshot_td, td_sink, trade_calendar

    excerpt = json.loads((REAL / 'c1312/td_excerpt.json').read_text())
    markers = [dict(zip(excerpt['marker_columns'], row)) for row in excerpt['markers']]
    rows = [json.loads(row[2]) for row in excerpt['rows']]
    selected = []

    class ReplaySnapshots:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read_latest(self, dataset):
            return {'trade_date': rows[0]['date'], 'rows': rows}

        def read_as_of(self, dataset, asof):
            assert dataset == 'stock_static'
            assert asof.endswith('T23:59:59+08:00')
            visible = [marker for marker in markers
                       if datetime.fromisoformat(marker['available_at']) <= datetime.fromisoformat(asof)]
            if not visible:
                return None
            marker = visible[0]
            selected.append(marker['version'])
            return {'trade_date': marker['available_at'][:10],
                    'rows': rows if marker is markers[0] else []}

    class Conn:
        def close(self):
            pass

    class Store:
        def publish(self, dataset, day, rows, metadata):
            return metadata

    monkeypatch.setattr(reference_snapshot_td, 'ReferenceSnapshotStore', ReplaySnapshots)
    monkeypatch.setattr(auction_redis_view, 'read_auction_view', lambda *args: [])
    monkeypatch.setattr(trade_calendar.TradeCalendarStore, 'all_trading_dates', lambda self: [])
    monkeypatch.setattr(td_sink, 'connect_td', lambda database: Conn())
    monkeypatch.setattr(auction_seal_facts, 'build_facts', lambda *args: {
        'rows': [], 'unknown_listing': [], 'unknown_prices': [], 'unlimited': []})
    for day, expected in [('2026-11-17', '20261116005011-39a46555d8c7'),
                          ('2026-11-13', '20261109005007-ea8cc8112d41')]:
        auction_seal_facts.run(day, redis_client=object(), store=Store())
        assert selected[-1] == expected
    with pytest.raises(ValueError, match='^stock_static_date_unavailable$'):
        auction_seal_facts.run('2026-11-08', redis_client=object(), store=Store())


def test_real_daily_view_price_source():
    quote = json.loads((REAL / 'c1326/view.json').read_text())
    with gzip.open(REAL / 'core_artifacts/c594/stock_static/2026-10-29/c573_guardian/rows.jsonl.gz', 'rt') as f:
        listings = {row['symbol']: row['listing_date'] for line in f if (row := json.loads(line))}
    tick_data = json.loads(next(REAL.glob('td_stock_tick_2026-11-16_002905_092430_092600_*.json')).read_text())
    ticks = [dict(zip(tick_data['columns'], row)) for row in tick_data['rows']]
    calendar = json.loads((REAL.parent / 'trade_calendar/baostock-2026-01-01_2026-12-31.json').read_text())
    dates = [r['calendar_date'] for r in calendar['rows'] if r['is_trading_day'] == '1']
    assert quote['source'] == 'redis_auction_full'
    result = build_facts('2026-11-16', {quote['symbol']: quote}, listings, dates, lambda _: ticks)
    assert result['rows'][0]['price_source'] == quote['src']
