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
        for day in data['intraday']:
            times = [p['time'] for p in day['timepoints']]
            if times != sorted(set(times)): errors.append(f"{day['trade_date']}: cuts not strictly ordered")
            for point in day['timepoints']:
                if 'outcomes' in point['factors']['auction_themes_5d']: errors.append('outcomes in features')
        print(json.dumps({'ok': not errors, 'errors': errors}, ensure_ascii=False))
        return 1 if errors else 0
    return 0
