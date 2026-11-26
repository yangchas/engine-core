"""Read-only bridge from TD and captured Kaipan data to research timelines."""
from collections import defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path

from .kaipan_cache import available_kaipan_plates, load_kaipan_cache, parse_historical_cohorts
from .td_reader import TDReader
from ..pipeline import build_timeseries_dataset


def _day(row):
    return str(row.get('trade_date') or row.get('ts'))[:10]


def _tick(row):
    result = {}
    for dest, source in [('px', 'px_milli'), ('pc', 'pc_milli'), ('open', 'o_milli'),
                         ('high', 'h_milli'), ('low', 'l_milli'), ('bid_price', 'bp1_milli'),
                         ('ask_price', 'ap1_milli')]:
        value = row.get(source)
        result[dest] = value / 1000 if value is not None else None
    result.update(amount_yuan=row.get('amt_yuan'), bid_volume=row.get('bv1'), ask_volume=row.get('av1'))
    return result


def _close(row):
    return {'px': row.get('close'), 'pc': row.get('pre_close'), 'open': row.get('open'),
            'high': row.get('high'), 'low': row.get('low'), 'pct': row.get('pct_chg'),
            'amount_yuan': row.get('amount_yuan')}


def build_from_td_and_cache(td_config, kaipan_cache, kaipan_live_root, *, allow_retrospective=True, reader=None):
    """Build an offline dataset; historical source availability is explicitly labelled.

    A reader supplies read(dataset, start, end), read_ticks(day, cutoff), and
    read_members(asof, retrospective). All TD calls are reads.
    """
    reader = reader or TDReader(config=str(td_config))
    history = load_kaipan_cache(kaipan_cache)
    dates = sorted({str(r['trade_date']) for r in history if r.get('trade_date') and r.get('his_plates') is not None})
    if not dates:
        raise ValueError('no historical Kaipan trading dates')
    live = []
    for path in sorted(Path(kaipan_live_root).glob('kaipan-hot-capture-20*')):
        if path.is_dir():
            live.extend(load_kaipan_cache(path))
    tables = {}
    for name in ('daily_kline', 'daily_dde', 'chip', 'price_volume'):
        by_date = defaultdict(dict)
        for offset in range(0, len(dates), 10):
            for row in reader.read(name, dates[offset], dates[min(offset + 9, len(dates)-1)]):
                d, symbol = _day(row), str(row.get('symbol') or '')
                if symbol:
                    by_date[d][symbol] = row
        tables[name] = by_date
    latest_members = reader.read_members(None, True) if allow_retrospective else None
    shared_members = defaultdict(list)
    for row in (latest_members or {}).get('rows', []):
        if row.get('plate_code') and row.get('symbol'):
            shared_members[str(row['plate_code'])].append(str(row['symbol']))
    days = {}
    for i, day in enumerate(dates):
        prev = dates[i-1] if i else None
        member_snapshot = latest_members if allow_retrospective else reader.read_members(day + 'T09:25:00+08:00', False)
        members = shared_members if allow_retrospective else defaultdict(list)
        if not allow_retrospective:
            for row in (member_snapshot or {}).get('rows', []):
                if row.get('plate_code') and row.get('symbol'):
                    members[str(row['plate_code'])].append(str(row['symbol']))
        prior = {}
        if prev:
            for symbol, k in tables['daily_kline'].get(prev, {}).items():
                pv = tables['price_volume'].get(prev, {}).get(symbol, {})
                chip = tables['chip'].get(prev, {}).get(symbol, {})
                if not allow_retrospective:
                    cutoff = datetime.fromisoformat(day + 'T09:25:00+08:00')
                    def visible(row):
                        try:
                            t = datetime.fromisoformat(row.get('available_at', ''))
                            return t.tzinfo is not None and t <= cutoff
                        except (TypeError, ValueError):
                            return False
                    pv = pv if visible(pv) else {}
                    chip = chip if visible(chip) else {}
                ma = pv.get('ma20'); close = k.get('close')
                bars = [tables['daily_kline'].get(d, {}).get(symbol, {}) for d in dates[max(0, i-20):i]]
                highs = [r.get('high') for r in bars if r.get('high') is not None]
                lows = [r.get('low') for r in bars if r.get('low') is not None]
                pos20 = ((close - min(lows)) / (max(highs) - min(lows)) * 100
                         if len(highs) == len(lows) == 20 and close is not None and max(highs) > min(lows) else None)
                dde_rows = [tables['daily_dde'].get(d, {}).get(symbol, {}).get('ddje') for d in dates[max(0, i-5):i]]
                prior[symbol] = {'ma20': ma, 'pos20': pos20, 'chip_profit_ratio': chip.get('profit_ratio'),
                    'above_ma20': close > ma if close is not None and ma is not None else None,
                    'dde5_yuan': sum(dde_rows) if len(dde_rows) == 5 and all(v is not None for v in dde_rows) else None}
        snapshots = {}
        for cut, clock in [('09:25', '09:25:00'), ('09:35', '09:35:00'), ('09:45', '09:45:30')]:
            rows = reader.read_ticks(day, clock)
            normalized = {}
            for row in rows:
                try:
                    stamp = datetime.fromisoformat(str(row.get('ts')))
                except ValueError:
                    continue
                if stamp.date().isoformat() != day or stamp.time() > time.fromisoformat(clock):
                    continue
                symbol = str(row.get('symbol') or '')
                if symbol:
                    normalized[symbol] = {**_tick(row), **{k: v for k, v in prior.get(symbol, {}).items() if k in ('ma20', 'chip_profit_ratio', 'pos20')}}
            if normalized:
                snapshots[cut] = normalized
        close_rows = {s: _close(r) for s, r in tables['daily_kline'].get(day, {}).items()}
        if close_rows:
            snapshots['15:00'] = close_rows
        hot = {}
        if prev:
            if allow_retrospective:
                matching = [r for r in history if r.get('trade_date') == prev]
                # The event date remains strictly before the target day.
                if matching:
                    hot['prev_close'] = available_kaipan_plates(matching, prev, '9999-12-31T23:59:59+08:00')
            else:
                hot['prev_close'] = available_kaipan_plates(history, prev, day + 'T09:25:00+08:00')
        for cut, clock in [('09:25', '09:25:00'), ('09:45', '09:45:30')]:
            hot[cut] = available_kaipan_plates(live, day, day + 'T' + clock)
        matching = next((r for r in history if r.get('trade_date') == prev), None)
        cohorts = parse_historical_cohorts(matching) if matching and allow_retrospective else None
        days[day] = {'snapshots': snapshots, 'members': members, 'prior_features': prior, 'kaipan': hot,
                     'prior_official_cohorts': cohorts}
    # The final calendar day's next-day auction is an outcome only.  Do not
    # skip missing dates and silently claim a later observation is D+1.
    next_day = (datetime.fromisoformat(dates[-1]) + timedelta(days=1)).date().isoformat()
    next_open = {}
    for row in reader.read_ticks(next_day, '09:25:00'):
        try:
            stamp = datetime.fromisoformat(str(row.get('ts')))
        except ValueError:
            continue
        if stamp.date().isoformat() == next_day and stamp.time() <= time(9, 25) and row.get('symbol'):
            next_open[str(row['symbol'])] = _tick(row)
    data = build_timeseries_dataset(days)
    # Close is used internally only to form the next day's cohorts, never a morning feature.
    for i, day in enumerate(data['intraday']):
        close = next((p for p in day['timepoints'] if p['time'] == '15:00'), None)
        day['timepoints'] = [p for p in day['timepoints'] if p['time'] != '15:00']
        previous = dates[i-1] if i else None
        hist = next((r for r in history if r.get('trade_date') == previous), None)
        if hist and (allow_retrospective or available_kaipan_plates([hist], previous, day['trade_date'] + 'T09:25:00+08:00') is not None):
            day['prior_kaipan_pools'] = {k: hist.get(k) for k in ('his_bans', 'his_zha', 'his_floor')}
        day['outcomes'] = {'close': close, 'next_open': (days[dates[i+1]]['snapshots'].get('09:25') if i+1 < len(dates) else next_open or None)}
    data['meta'].update(provenance='RETROSPECTIVE_BACKFILL' if allow_retrospective else 'STRICT_CAPTURE_TIME',
                        source_dates={'first': dates[0], 'last': dates[-1]},
                        membership={k: v for k, v in (latest_members or {}).items() if k != 'rows'})
    return data
