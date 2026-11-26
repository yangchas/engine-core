"""JSON-only offline CLI: build from normalized inputs, inspect or audit results."""
import argparse
import json
import gzip
import pickle
from pathlib import Path
from .pipeline import build_timeseries_dataset

def _load(path):
    if str(path).endswith('.pkl.gz'):
        with gzip.open(path, 'rb') as source:
            return pickle.load(source)
    return json.loads(Path(path).read_text(encoding='utf-8'))

def main(argv=None):
    parser = argparse.ArgumentParser(description='Build and inspect point-in-time sentiment research datasets')
    sub = parser.add_subparsers(dest='command', required=True)
    build = sub.add_parser('build', help='build dataset from normalized JSON day records')
    build.add_argument('input'); build.add_argument('output')
    real = sub.add_parser('build-real', help='build from read-only TD and Kaipan captures')
    real.add_argument('--td-config', required=True); real.add_argument('--kaipan-cache', required=True)
    real.add_argument('--kaipan-live-root', required=True); real.add_argument('--out', required=True)
    real.add_argument('--strict-capture-time', action='store_true')
    inspect = sub.add_parser('inspect', help='print one day from a dataset')
    inspect.add_argument('dataset'); inspect.add_argument('date')
    audit = sub.add_parser('audit', help='check dataset chronology and feature/outcome separation')
    audit.add_argument('dataset')
    args = parser.parse_args(argv)
    if args.command == 'build':
        data = build_timeseries_dataset(json.loads(Path(args.input).read_text(encoding='utf-8')))
        Path(args.output).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    elif args.command == 'build-real':
        from .adapters.real_loader import build_from_td_and_cache
        data = build_from_td_and_cache(args.td_config, args.kaipan_cache, args.kaipan_live_root, allow_retrospective=not args.strict_capture_time)
        with gzip.open(args.out, 'wb') as out:
            pickle.dump(data, out, protocol=pickle.HIGHEST_PROTOCOL)
        print(json.dumps(data['meta'], ensure_ascii=False))
    elif args.command == 'inspect':
        data = _load(args.dataset)
        found = [d for d in data['intraday'] if d['trade_date'] == args.date]
        if not found: parser.error('date not found')
        print(json.dumps(found[0], ensure_ascii=False, indent=2))
    else:
        data = _load(args.dataset)
        errors = []
        dates = [d['trade_date'] for d in data['intraday']]
        if dates != sorted(set(dates)): errors.append('dates not strictly ordered')
        if data['meta'].get('intraday_tick_days') != sum(d.get('has_intraday_ticks') is True for d in data['intraday']): errors.append('intraday tick day count mismatch')
        if data['meta'].get('daily_days') != len(data['daily']): errors.append('daily day count mismatch')
        for day in data['intraday']:
            if not isinstance(day.get('has_intraday_ticks'), bool): errors.append(f"{day['trade_date']}: missing tick flag")
            elif day['has_intraday_ticks'] != any(p['time'] != '15:00' for p in day['timepoints']): errors.append(f"{day['trade_date']}: tick flag mismatch")
            times = [p['time'] for p in day['timepoints']]
            if times != sorted(set(times)): errors.append(f"{day['trade_date']}: cuts not strictly ordered")
            for point in day['timepoints']:
                if 'outcomes' in point['factors']['auction_themes_5d']: errors.append('outcomes in features')
        coverage = {}
        cuts = {'15:00': data['daily']}
        for cut in ('09:25', '09:35', '09:45'):
            cuts[cut] = [p for d in data['intraday'] for p in d['timepoints'] if p['time'] == cut]
        for cut, points in cuts.items():
            checks = {
                'pos20_med': lambda f: f['market_global'].get('pos20_med') is not None,
                'above_ma20_pct': lambda f: f['market_global'].get('above_ma20_pct') is not None,
                'chip_prof_med': lambda f: f['market_global'].get('chip_prof_med') is not None,
                'yw_pp': lambda f: f['market_global'].get('yw_pp') is not None,
                'top_amount_promo_pct': lambda f: f['top_amount_core'].get('promo_pct') is not None,
                **{f'{group}_promo_pct': (lambda f, group=group: (f['broken_rotten'].get(group) or {}).get('promo_pct') is not None) for group in ('broken', 'rotten', 'floor')},
            }
            if cut == '15:00':
                checks['today_close'] = lambda f: f['kaipan_hot'].get('today_close') is not None
            if cut == '09:35':
                checks['today_0935'] = lambda f: f['kaipan_hot'].get('today_0935') is not None
            if cut != '15:00':
                checks['prev_hot_member_perf'] = lambda f: (f['kaipan_hot'].get('prev_hot_member_perf') or {}).get('avg_pct') is not None
            if cut == '09:45':
                checks['amt45_yi'] = lambda f: any(x.get('amt45_yi') is not None for x in f['auction_themes_5d']['features'])
            coverage[cut] = {name: {'present': sum(bool(fn(p['factors'])) for p in points), 'total': len(points)} for name, fn in checks.items()}
            for name, counts in coverage[cut].items():
                if counts['total'] and not counts['present']:
                    errors.append(f'{cut}: {name} has zero coverage')
        print(json.dumps({'ok': not errors, 'errors': errors, 'coverage': coverage}, ensure_ascii=False))
        return 1 if errors else 0
    return 0
