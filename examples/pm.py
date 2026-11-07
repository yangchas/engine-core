#!/usr/bin/env python3
"""Read-only PM commands."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo


TERMINAL = frozenset({'SUCCEEDED', 'SUCCEEDED_DEGRADED', 'FAILED_FINAL',
                      'MISSED', 'SKIPPED_NA', 'SKIPPED_PHASE'})


def latest_state(lines, event_id):
    latest = None
    for line in lines:
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        data = entry.get('data')
        if (entry.get('type') == 'INSTANCE_STATE' and isinstance(data, dict)
                and data.get('event_id') == event_id and isinstance(data.get('state'), dict)):
            latest = data['state']
    return latest


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    wait = sub.add_parser('wait')
    wait.add_argument('--ledger-event', required=True)
    wait.add_argument('--date', default=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat())
    wait.add_argument('--timeout', type=float, default=1700)
    wait.add_argument('--interval', type=float, default=20)
    wait.add_argument('--state-root', type=Path, default=Path.home() / 'core-data/guardian-native')
    args = parser.parse_args(argv)
    if args.timeout < 0 or args.interval <= 0:
        parser.error('timeout must be nonnegative and interval must be positive')
    try:
        datetime.strptime(args.date, '%Y-%m-%d')
    except ValueError:
        parser.error('date must be YYYY-MM-DD')
    ledger = args.state_root / 'state' / 'ledger' / (args.date + '.jsonl')
    deadline = time.monotonic() + args.timeout
    while True:
        try:
            with ledger.open(encoding='utf-8') as lines:
                state = latest_state(lines, args.ledger_event)
        except FileNotFoundError:
            state = None
        status = state.get('status', 'NONE') if state is not None else 'NONE'
        if status in TERMINAL:
            print(f"{args.ledger_event} {status} completed_at={state.get('completed_at', '')} last_error={str(state.get('last_error', ''))[:120]}")
            return 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f'TIMEOUT {args.ledger_event} last_status={status}')
            return 2
        time.sleep(min(args.interval, remaining))


if __name__ == '__main__':
    raise SystemExit(main())
