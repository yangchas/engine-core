#!/usr/bin/env python3
"""Read-only post-deployment health verdict and suggested rollback command."""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LOCAL = ZoneInfo('Asia/Shanghai')
LIVE = Path('/home/exedev/repos/engine_core-live')
RELEASES = Path.home() / 'fw/evidence/release'
FAILURES = {'FAILED_FINAL', 'MISSED'}


def timestamp(value: str | datetime) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return dt.replace(tzinfo=LOCAL) if dt.tzinfo is None else dt


def evaluate(deploy_at: datetime, live_sha: str, expected_sha: str, heartbeat: dict,
             expected_config_version: int | None, today_events: dict, prior_events: dict,
             guardiand_running: bool) -> dict:
    deploy_at = timestamp(deploy_at)
    reasons = []
    if not guardiand_running:
        reasons.append('guardiand is not running')
    if live_sha != expected_sha:
        reasons.append('live sha differs from deployed sha')
    if expected_config_version is not None and heartbeat.get('config_version') != expected_config_version:
        reasons.append('heartbeat config version differs from expected')
    new_failures = []
    completed = False
    for event_id, event in today_events.items():
        end = event.get('completed_at')
        if end and timestamp(end) >= deploy_at:
            completed = True
        when = end or event.get('started_at')
        if (event.get('status') in FAILURES and when and timestamp(when) >= deploy_at
                and prior_events.get(event_id, {}).get('status') not in FAILURES):
            new_failures.append(event_id)
    new_failures.sort()
    if new_failures:
        reasons.append('new event failures after deployment')
    if reasons:
        verdict = 'RED'
    elif not completed:
        verdict = 'WAIT'
        reasons.append('no event completed after deployment')
    else:
        verdict = 'GREEN'
    return {'verdict': verdict, 'reasons': reasons, 'new_failures': new_failures}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def guardian_pids(root: Path) -> list[int]:
    executable = str((root / 'guardiand').resolve())
    state = str((root / 'state').resolve())
    found = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            args = (proc / 'cmdline').read_bytes().decode().split('\0')
            if (args[0] == executable and '--state-root' in args
                    and str(Path(args[args.index('--state-root') + 1]).resolve()) == state):
                found.append(int(proc.name))
        except (OSError, UnicodeError, IndexError):
            continue
    return sorted(found)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deploy-record', type=Path)
    parser.add_argument('--state-root', type=Path, default=Path.home() / 'core-data/guardian-native')
    parser.add_argument('--now', help='observation time as ISO datetime')
    parser.add_argument('--expected-config-version', type=int)
    parser.add_argument('--config-prev', type=Path, help='configuration backup to restore on rollback')
    args = parser.parse_args()
    record_path = args.deploy_record
    if record_path is None:
        records = sorted(RELEASES.glob('deploy-*.json'))
        if not records:
            parser.error('no deployment record found')
        record_path = records[-1]
    record = read_json(record_path)
    # release.py encodes its deployment time in the filename, not the JSON.
    deploy_at = timestamp(record['at']) if record.get('at') else timestamp(
        datetime.strptime(record_path.stem.removeprefix('deploy-'), '%Y%m%dT%H%M%S'))
    now = timestamp(args.now) if args.now else datetime.now(LOCAL)
    calendar = json.loads((args.state_root.parent / 'trade_dates.json').read_text(encoding='utf-8'))
    previous = max(day for day in calendar if day < now.date().isoformat())
    ledger = args.state_root / 'state/ledger'
    today_path = ledger / f'snapshot-{now.date().isoformat()}.json'
    today = read_json(today_path)['events'] if today_path.exists() else {}
    prior = read_json(ledger / f'snapshot-{previous}.json')['events']
    heartbeat = read_json(args.state_root / 'state/heartbeat.json')
    sha = subprocess.run(['git', '-C', str(LIVE), 'rev-parse', 'HEAD'], check=True,
                         capture_output=True, text=True).stdout.strip()
    pids = guardian_pids(args.state_root)
    result = evaluate(deploy_at, sha, record['deployed'], heartbeat, args.expected_config_version,
                      today, prior, bool(pids))
    if result['verdict'] == 'RED':
        if args.config_prev:
            copy = f"cp -- {shlex.quote(str(args.config_prev))} {shlex.quote(str(args.state_root / 'config.json'))}"
            kill = 'kill -TERM ' + ' '.join(map(str, pids)) if pids else '# guardiand is not running; no PID to signal'
            result['rollback'] = f'{copy} && {kill}' if pids else f'{copy}\n{kill}'
        else:
            result['rollback'] = "release.py rollback --go --authorized-by 'deploy_watch'"
    print(json.dumps(result, ensure_ascii=False))
    return {'GREEN': 0, 'WAIT': 2, 'RED': 3}[result['verdict']]


if __name__ == '__main__':
    raise SystemExit(main())
