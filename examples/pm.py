#!/usr/bin/env python3
"""PM workflow commands."""
import argparse
from datetime import datetime
import json
import os
import re
import subprocess
from pathlib import Path
import time
from zoneinfo import ZoneInfo


TERMINAL = frozenset({'SUCCEEDED', 'SUCCEEDED_DEGRADED', 'FAILED_FINAL',
                      'MISSED', 'SKIPPED_NA', 'SKIPPED_PHASE'})


ROOT = Path('/home/exedev/repos/engine_core')
CARDS = Path('/home/exedev/fw/cards')
PYTHON = '/home/exedev/services/engine-next/shared/venv/bin/python'
CALLBACK = '/home/exedev/fw/bin/card_callback'


def worktree_from_card(text):
    match = re.search(r'^SCOPE_WRITE:[^\n]*', text, re.MULTILINE)
    if match:
        for path in match.group().split(':', 1)[1].split(','):
            root = re.match(r'\s*(/[^\s,]*?-wt-[^/\s,]+)(?:/|\s|$)', path)
            if root:
                return root.group(1)
    return None


def set_status(text, new_line):
    return re.sub(r'^STATUS:[^\r\n]*', lambda _: new_line, text, count=1, flags=re.MULTILINE)


def accept(args):
    if not re.fullmatch(r'C\d+', args.card):
        print(f'ACCEPT {args.card} REFUSED setup: invalid card ID')
        return 2
    card_path = CARDS / f'{args.card}.md'
    try:
        card_text = card_path.read_text(encoding='utf-8')
        worktree = worktree_from_card(card_text)
        if not worktree:
            raise ValueError('no worktree in SCOPE_WRITE')
        def git(repo, *command):
            return subprocess.run(['git', '-C', str(repo), *command], check=True,
                                  capture_output=True, text=True).stdout.strip()
        branch = git(worktree, 'branch', '--show-current')
        if not branch:
            raise ValueError('worktree has detached HEAD')
        base = git(ROOT, 'merge-base', 'HEAD', branch)
        if not re.search(r'^STATUS:', card_text, re.MULTILINE):
            raise ValueError('card has no STATUS line')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'ACCEPT {args.card} REFUSED setup: {exc}')
        return 2
    audit_cmd = [PYTHON, str(ROOT / 'examples/audit_card.py'), '--card', args.card,
                 '--branch', branch, '--base', base, '--repo', str(ROOT)]
    merge_cmd = [PYTHON, str(ROOT / 'examples/release.py'), 'merge', '--branch', branch, '--card', args.card]
    if args.dry_run:
        print(f'worktree {worktree} branch {branch} base {base}')
        print('audit skipped' if args.no_audit else 'audit ' + ' '.join(audit_cmd))
        print('merge ' + ' '.join(merge_cmd))
        print(f'status {card_path} DONE(已合并 <sha>)')
        print(f'callback {CALLBACK} --ack {args.card}')
        return 0
    if not args.no_audit:
        try:
            result = subprocess.run(audit_cmd, capture_output=True, text=True, timeout=900)
            payload = json.loads(result.stdout)
            problems = payload.get('problems', [])
            if result.returncode or payload.get('verdict') != 'PASS':
                detail = '; '.join(map(str, problems)) or result.stderr.strip() or 'audit failed'
                print(f'ACCEPT {args.card} REFUSED audit: {detail[:120]}')
                return 3
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f'ACCEPT {args.card} REFUSED audit: {str(exc)[:120]}')
            return 3
    try:
        result = subprocess.run(merge_cmd, capture_output=True, text=True, timeout=900,
                                env={**os.environ, 'TMPDIR': '/tmp'})
        if result.returncode:
            lines = (result.stdout + result.stderr).splitlines()
            detail = lines[-1] if lines else f'exit {result.returncode}'
            print(f'ACCEPT {args.card} REFUSED merge: {detail[:150]}')
            return 4
    except (OSError, subprocess.SubprocessError) as exc:
        print(f'ACCEPT {args.card} REFUSED merge: {str(exc)[:150]}')
        return 4
    try:
        sha = git(ROOT, 'rev-parse', '--short', 'HEAD')
        current_text = card_path.read_text(encoding='utf-8')
        if not re.search(r'^STATUS:', current_text, re.MULTILINE):
            raise ValueError('card has no STATUS line')
        card_path.write_text(set_status(current_text, f'STATUS: DONE(已合并 {sha})'), encoding='utf-8')
        result = subprocess.run([CALLBACK, '--ack', args.card], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f'exit {result.returncode}')
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'ACCEPT {args.card} MERGED; status/callback failed: {exc}')
        return 5
    print(f"ACCEPT {args.card} MERGED {sha} audit={'SKIPPED' if args.no_audit else 'PASS'}")
    return 0


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
    accept_parser = sub.add_parser('accept')
    accept_parser.add_argument('card')
    accept_parser.add_argument('--dry-run', action='store_true')
    accept_parser.add_argument('--no-audit', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'accept':
        return accept(args)
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
