"""Read-only loading of research artifacts; no market writes or broker access."""
import gzip
import pickle
from .contracts import MarketSnapshot


def load_dataset(path):
    # Research pickle files must be trusted: unpickling can execute code.
    with gzip.open(path, 'rb') as stream:
        return pickle.load(stream)


def raw_from_point(point):
    factors = point['factors']
    market, ext, ladder = factors['market_global'], factors['extremes'], factors['ladder_relay']
    two = ladder['2p']['avg_pct']
    return {'trade_date': point['trade_date'], 'time': '09:45',
            'up_ratio': market.get('up_ratio'), 'net_lu': ext['sealed_up'] - ext['sealed_down'],
            'top100_avg_pct': factors['top_amount_core'].get('avg_pct'),
            'yest_2p_avg_pct': two if two is not None else ladder['1b']['avg_pct'],
            'high_minus_low_pp': factors['leader_divergence'].get('high_minus_low_pp'),
            'yw_pp': market.get('yw_pp'), 'pos20_med': market.get('pos20_med'),
            'gap': factors['theme_competition'].get('top1_vs_top2_gap_pp')}


def history_records(dataset):
    records = []
    for day in dataset['intraday']:
        point = next((p for p in day['timepoints'] if p['time'] == '09:45'), None)
        if point:
            records.append(raw_from_point(point))
    return records


def snapshots_from_dataset(dataset, reader):
    """Convert an explicitly normalized snapshot dataset.

    Sentiment factor datasets alone do not contain candidate membership, names,
    09:25 quotes or prior landmine facts. Reject them rather than invent them.
    """
    records = dataset.get('strategy_snapshots')
    if records is None:
        raise ValueError('dataset lacks strategy_snapshots: candidate membership, names, 09:25 quotes and prior landmine facts are required')
    for record in records:
        yield MarketSnapshot(**record)


def extract_regime_history(dataset):
    return sorted(history_records(dataset), key=lambda row: row['trade_date'])


def _read_names(path):
    import json
    if path is None:
        paths = sorted(__import__('pathlib').Path('/home/exedev/core-data/stock_static').glob('trade_date=2026-10-29/**/rows.jsonl.gz'))
        path = paths[0] if paths else None
    if not path:
        return {}
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        return {r['symbol']: r.get('name') for r in map(json.loads, stream) if r.get('symbol')}


def _members(snapshot):
    from collections import defaultdict
    members, names = defaultdict(set), defaultdict(set)
    for row in (snapshot or {}).get('rows', []):
        symbol = str(row.get('symbol') or '')
        label = str(row.get('plate_member_label') or row.get('plate_name') or '').replace('、', ',').split(',')[0].strip()
        if symbol and label:
            members[label].add(symbol)
        name = row.get('stock_name') or row.get('symbol_name')
        if symbol and name:
            names[symbol].add(str(name))
    return members, {s: next(iter(v)) for s, v in names.items() if len(v) == 1}


def _cohorts(cache, previous, cutoff=None):
    from datetime import datetime
    from engine_core.sentiment_review.adapters.kaipan_cache import parse_historical_cohorts
    records = [r for r in cache if r.get('trade_date') == previous and r.get('his_bans') is not None]
    if cutoff:
        def available(r):
            try:
                return datetime.fromisoformat(r['available_at']) <= datetime.fromisoformat(cutoff)
            except (KeyError, TypeError, ValueError):
                return False
        records = [r for r in records if available(r)]
    return parse_historical_cohorts(records[-1]) if records else {}


def _ticks(reader, day, cut):
    rows = {}
    for row in reader.read_ticks(day, cut):
        symbol, stamp = str(row.get('symbol') or ''), str(row.get('ts') or '')
        if not symbol or stamp[:10] != day or stamp[11:19] > cut:
            continue
        if symbol in rows:
            raise ValueError(f'duplicate tick: {day} {symbol}')
        rows[symbol] = row
    return rows


def _candidates(members, names, ticks25, ticks45, cohorts, themes):
    from engine_core.sentiment_review.adapters.real_loader import _tick
    from engine_core.sentiment_review.factors._common import pct
    streaks, broken = cohorts.get('official_streaks'), cohorts.get('official_broken_symbols')
    candidates = []
    ranked = sorted((t for t in themes if t.get('p45') is not None), key=lambda t: (-t['p45'], t['theme']))
    selected_themes = {t['theme'] for t in ranked[:3]}
    for theme, symbols in members.items():
        if theme not in selected_themes:
            continue
        for symbol in sorted(symbols):
            row = ticks45.get(symbol, {})
            p, hi, lo = row.get('px_milli'), row.get('h_milli'), row.get('l_milli')
            pos = (p-lo)/(hi-lo) if all(isinstance(x, (int, float)) and x > 0 for x in (p, hi, lo)) and hi > lo else None
            def valid_pct(tick):
                px, pc = tick.get('px_milli'), tick.get('pc_milli')
                return pct(_tick(tick)) if px is not None and pc is not None and px > 0 and pc > 0 else None
            candidates.append(dict(symbol=symbol, name=names.get(symbol), theme=theme,
                ret_25=valid_pct(ticks25.get(symbol, {})), ret_45=valid_pct(row),
                intraday_pos_45=pos, amt_45_yuan=row.get('amt_yuan'),
                ask1=row['ap1_milli']/1000 if row.get('ap1_milli') is not None else None,
                av1=row.get('av1'), prev_streak=streaks.get(symbol, 0) if streaks is not None else None,
                prev_broken=int(symbol in broken) if broken is not None else None))
    return candidates


def _bars(reader, start, end):
    from collections import defaultdict
    result = defaultdict(dict)
    for row in reader.read('daily_kline', start, end):
        day, symbol = str(row.get('ts') or row.get('trade_date'))[:10], str(row.get('symbol') or '')
        if symbol in result[day]:
            raise ValueError(f'duplicate daily bar: {day} {symbol}')
        if symbol:
            result[day][symbol] = row
    return result


def load_backtest_snapshots(dataset_path, td_config, kaipan_cache_dir, stock_static_path=None):
    from datetime import date, timedelta
    from engine_core.sentiment_review.adapters.td_reader import TDReader
    from engine_core.sentiment_review.adapters.kaipan_cache import load_kaipan_cache
    data = load_dataset(dataset_path)
    days = sorted((d for d in data['intraday'] if any(p['time'] == '09:45' for p in d['timepoints'])), key=lambda d: d['trade_date'])
    if not days:
        raise ValueError('no 09:45 dataset records')
    reader, cache = TDReader(config=td_config), load_kaipan_cache(kaipan_cache_dir)
    first, last = days[0]['trade_date'], days[-1]['trade_date']
    end = (date.fromisoformat(last) + timedelta(days=7)).isoformat()
    bars = _bars(reader, (date.fromisoformat(first) - timedelta(days=90)).isoformat(), end)
    dates = sorted(bars)
    later = [d for d in dates if d > last]
    if not later:
        raise ValueError('missing next trading day for settlement')
    members, member_names = _members(reader.read_members(None, True))
    names = {**member_names, **{k: v for k, v in _read_names(stock_static_path).items() if v}}
    snapshots = []
    for day in days:
        d = day['trade_date']
        prior = [x for x in dates if x < d]
        if not prior: raise ValueError(f'missing prior daily bars for {d}')
        prev = prior[-1]
        p = next(p for p in day['timepoints'] if p['time'] == '09:45')
        t25, t45 = _ticks(reader, d, '09:25:00'), _ticks(reader, d, '09:45:30')
        if not t25 or not t45 or not bars.get(d): raise ValueError(f'missing quotes or close for {d}')
        themes = [r for r in p['factors']['auction_themes_5d']['features'] if r.get('n', 0) >= 5 and r.get('p45') is not None]
        snapshots.append(MarketSnapshot(d, prev, d+'T09:45:30+08:00', raw_from_point(p), themes,
            _candidates(members, names, t25, t45, _cohorts(cache, prev), themes), t45,
            {s: r.get('close') for s, r in bars[d].items()}))
    d = later[0]
    snapshots.append(MarketSnapshot(d, last, d+'T09:45:30+08:00', {}, [], [], _ticks(reader, d, '09:45:30'),
        {s: r.get('close') for s, r in bars[d].items()}))
    if not snapshots[-1].quotes_0945: raise ValueError('missing settlement quotes')
    return snapshots


def load_live_shadow_snapshot(trade_date, td_config, kaipan_cache_dir=None, stock_static_path=None):
    from datetime import date, timedelta
    from engine_core.sentiment_review.adapters.td_reader import TDReader
    from engine_core.sentiment_review.adapters.kaipan_cache import load_kaipan_cache
    from engine_core.sentiment_review.adapters.real_loader import _tick, _close
    from engine_core.sentiment_review.factors import (compute_market_global_factor, compute_extremes_factor,
        compute_ladder_relay_factor, compute_top_amount_core_factor, compute_leader_divergence_factor,
        compute_auction_themes_5d_factor, compute_theme_competition_factor)
    reader = TDReader(config=td_config)
    day = date.fromisoformat(trade_date)
    bars = _bars(reader, (day-timedelta(days=90)).isoformat(), (day-timedelta(days=1)).isoformat())
    dates = sorted(d for d in bars if d < trade_date)
    if not dates: raise ValueError('missing prior daily bars')
    prev = dates[-1]
    cutoff = trade_date+'T09:45:30+08:00'
    member_snapshot = reader.read_members(cutoff, False)
    fallback = not member_snapshot or not member_snapshot.get('rows')
    if fallback: member_snapshot = reader.read_members(None, True)
    members, member_names = _members(member_snapshot)
    names = {**member_names, **{k: v for k, v in _read_names(stock_static_path).items() if v}}
    cache = load_kaipan_cache(kaipan_cache_dir) if kaipan_cache_dir else []
    cohorts = _cohorts(cache, prev, cutoff)
    streaks = cohorts.get('official_streaks')
    t25, t45 = _ticks(reader, trade_date, '09:25:00'), _ticks(reader, trade_date, '09:45:30')
    if not t25 or not t45: raise ValueError('missing intraday ticks')
    rows = {}
    for symbol, tick in t45.items():
        k = bars[prev].get(symbol, {})
        amount, turnover = k.get('amount_yuan'), k.get('turnover_pct')
        cap = amount/(turnover/100) if amount is not None and turnover is not None and amount > 0 and turnover > 0 else None
        window = [bars[d].get(symbol, {}) for d in dates[-20:]]
        highs, lows = [r.get('high') for r in window], [r.get('low') for r in window]
        close = k.get('close')
        pos = 100*(close-min(lows))/(max(highs)-min(lows)) if len(window) == 20 and close is not None and all(x is not None for x in highs+lows) and max(highs)>min(lows) else None
        rows[symbol] = {**_tick(tick), 'float_cap': cap, 'pos20': pos}
    normalized25 = {s: _tick(r) for s, r in t25.items()}
    ext = compute_extremes_factor(rows)
    market = compute_market_global_factor(rows)
    top = compute_top_amount_core_factor(rows, {s: _close(r) for s, r in bars[prev].items()}, ext['sets']['sealed_up'])
    ladder = compute_ladder_relay_factor(rows, streaks or {}, ext['sets']['sealed_up'])
    divergence = compute_leader_divergence_factor(streaks or {}, rows, ext['sets']['sealed_up'])
    themes = compute_auction_themes_5d_factor(members, {'09:25': normalized25, '09:45': rows})['features']
    gap = compute_theme_competition_factor(themes, None, time='09:45')['top1_vs_top2_gap_pp']
    raw = {'up_ratio': market['up_ratio'], 'net_lu': ext['sealed_up']-ext['sealed_down'],
           'top100_avg_pct': top['avg_pct'], 'yest_2p_avg_pct': (ladder['2p']['avg_pct'] if ladder['2p']['avg_pct'] is not None else ladder['1b']['avg_pct']) if streaks is not None else None,
           'high_minus_low_pp': divergence['high_minus_low_pp'] if streaks is not None else None,
           'yw_pp': market['yw_pp'], 'pos20_med': market['pos20_med'], 'gap': gap}
    return MarketSnapshot(trade_date, prev, cutoff, raw, themes, _candidates(members, names, t25, t45, cohorts, themes), t45,
                          source_metadata={'membership': 'latest_fallback' if fallback else 'as_of', 'kaipan': 'available_by_cutoff' if streaks is not None else 'unavailable',
                                           'stock_names': 'static_fallback_may_be_retrospective'})
