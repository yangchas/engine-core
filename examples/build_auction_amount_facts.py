#!/usr/bin/env python3
"""Acquire daily auction amount facts."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from engine_core.data_pipeline.auction_amount_facts import run


class ContractParser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({'contract': 1, 'status': 'failed', 'error_code': 'CONFIG_ERROR',
                          'retryable': False, 'detail': {'message': message}}))
        self.exit(64)


def main(argv=None):
    parser = ContractParser(description=__doc__)
    dates = parser.add_mutually_exclusive_group(required=True)
    dates.add_argument('--end-date')
    dates.add_argument('--days', nargs='+')
    parser.add_argument('--window', type=int, default=None)
    parser.add_argument('--max-days', type=int, default=5)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--available-at')
    parser.add_argument('--artifact-root', default=os.getenv('CORE_DATA_ROOT', '~/core-data'))
    args = parser.parse_args(argv)
    request = {'max_days': args.max_days}
    if args.days is not None:
        request['days'] = args.days
        if args.window is not None:
            request['window'] = args.window
    else:
        request.update(end_date=args.end_date, window=64 if args.window is None else args.window)
    if args.available_at is not None:
        request['available_at'] = args.available_at
    code, reply = run(request, args.artifact_root, dry_run=args.dry_run)
    print(json.dumps(reply, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
