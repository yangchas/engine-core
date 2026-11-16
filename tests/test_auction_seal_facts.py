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
    assert row['basis'] is None and row['seal_volume'] is None and row['seal_amount_yuan'] is None

