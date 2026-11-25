#!/usr/bin/env python3
"""Read-only first-day auction and opening checks."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from engine_core.data_pipeline.auction_redis_view import ViewError, read_auction_view

EVENTS = ('startup_0830', 'premarket_slice_0850', 'startup_0900', 'auction_0920_check',
          'auction_0924_check', 'auction_0925_check', 'auction_report_0926',
          'open_report_0932', 'open_report_0935', 'open_report_0940', 'open_report_0945')
TARGET_EXE = '/home/exedev/services/t1-v2/releases/20261113_C1047_52d11d9/content/bin/t1_v2'


def classify_schema(rows):
    values = rows.values() if isinstance(rows, dict) else rows
    values = list(values)
    return sum(isinstance(row, dict) and {'ask_amount_yuan', 'tick_ts_ms'} <= row.keys() for row in values), len(values)


def ledger_states(lines):
    found = {}
    for line in lines:
        try:
            entry = json.loads(line)
            data = entry.get('data', {})
            name = data.get('event_id') or data.get('instance')
            if name not in EVENTS:
                continue
            item = found.setdefault(name, {})
            if entry.get('type') == 'ATTEMPT_END':
                item['exit_code'] = data.get('exit_code')
                item['detail'] = str(data.get('reply', {}).get('detail', ''))
            elif entry.get('type') == 'INSTANCE_STATE':
                state = data.get('state', {})
                if state.get('status') in ('SUCCEEDED', 'SUCCEEDED_DEGRADED', 'FAILED_FINAL', 'MISSED', 'SKIPPED_NA', 'SKIPPED_PHASE', 'SKIPPED_SATISFIED'):
                    item['status'] = state['status']
                    item['detail'] = str(state.get('last_error') or item.get('detail', ''))
        except (ValueError, TypeError, AttributeError):
            continue
    for name, item in found.items():
        detail = item.get('detail', '')
        item['expected'] = (name == 'startup_0830' and 'PREP_CRITICAL_MISSING' in detail and 'auction_baseline' in detail
                            or name in ('auction_0920_check', 'auction_0924_check') and item.get('status') == 'SUCCEEDED_DEGRADED' and 'insufficient_snapshot' in detail)
    return found


def report_summary(manifest, report):
    quality = manifest.get('slices', {}).get('auction_stock_facts', {}).get('quality', {})
    sources = {tag: quality.get('source_quality', {}).get(tag, {}).get('source') for tag in ('0920', '0924', '0925')}
    matches = []
    def walk(obj, is_summary=False):
        if isinstance(obj, dict):
            if 'seal_label' in obj or is_summary:
                matches.append(obj)
            for key, value in obj.items():
                if isinstance(value, dict):
                    walk(value, key == 'summary')
    walk(report)
    label = next((x['seal_label'] for x in matches if 'seal_label' in x), quality.get('seal_label'))
    keys = ('VERIFIED', 'VERIFIED_REPLAY', 'NOT_LIMIT', 'UNKNOWN', 'LIMIT_NO_SEAL')
    counts = next(({k: x[k] for k in keys if k in x} for x in matches if any(k in x for k in keys)), None)
    if counts is None:
        for obj in matches + [quality]:
            if 'verified_live_n' in obj or 'verified_replay_n' in obj:
                counts = dict.fromkeys(keys)
                counts['VERIFIED'] = obj.get('verified_live_n')
                counts['VERIFIED_REPLAY'] = obj.get('verified_replay_n')
                break
    return {'sources': sources, 'seal_label': label, 'counts': counts}


class GetOnly:
    def __init__(self, client):
        self.client = client
    def mget(self, keys):
        return [self.client.get(key) for key in keys]


def run(day):
    results = []
    def add(status, name, detail):
        results.append((status, name, str(detail).replace('\n', ' ')))
    base = Path('/home/exedev/core-data')
    try:
        ledger = ledger_states((base / f'guardian-native/state/ledger/{day}.jsonl').read_text().splitlines())
    except OSError:
        ledger = {}
    for name in EVENTS:
        item = ledger.get(name, {})
        status = item.get('status')
        mark = 'EXPECTED' if item.get('expected') else ('PASS' if status in ('SUCCEEDED', 'SKIPPED_SATISFIED') else 'FAIL' if status in ('FAILED_FINAL', 'MISSED', 'SUCCEEDED_DEGRADED') else 'UNKNOWN')
        detail = item.get('detail', '')
        reason = ' PREP_CRITICAL_MISSING:auction_baseline' if 'PREP_CRITICAL_MISSING' in detail and 'auction_baseline' in detail else ' insufficient_snapshot' if 'insufficient_snapshot' in detail else ''
        add(mark, name, f"status={status or 'unavailable'} exit={item.get('exit_code', 'unknown')}{reason}")
    client = None
    try:
        import redis
        url = os.environ.get('REDIS_URL')
        if url:
            client = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=3,
                                         lib_name=None, lib_version=None)
    except Exception:
        pass
    for tag in ('0920', '0924', '0925'):
        key = f'core:auction:full:{day}:{tag}'
        if client is None:
            for name in ('publication', 'schema', 'core'):
                add('UNKNOWN', f'{tag} {name}', 'Redis unavailable')
            continue
        start = len(results)
        try:
            exists, meta_exists = client.exists(key), client.exists(key + ':meta')
            if not exists or not meta_exists:
                add('FAIL', f'{tag} publication', f'key={exists} meta={meta_exists}')
                add('UNKNOWN', f'{tag} schema', 'missing publication')
            else:
                ttl, meta_ttl, length = client.ttl(key), client.ttl(key + ':meta'), client.strlen(key)
                payload, meta_raw = client.get(key), client.get(key + ':meta')
                rows, meta = json.loads(payload), json.loads(meta_raw)
                if not isinstance(rows, dict) or not isinstance(meta, dict):
                    raise ValueError('invalid_structure')
                digest = hashlib.sha256(payload).hexdigest() == meta.get('sha256')
                count_ok = type(meta.get('rows')) is int and len(rows) == meta['rows']
                add('PASS' if ttl > 0 and meta_ttl > 0 and digest and count_ok else 'FAIL', f'{tag} publication', f'key={exists} meta={meta_exists} ttl={ttl}/{meta_ttl} bytes={length} sha256={digest} rows={len(rows)}/{meta.get("rows")}')
                new, total = classify_schema(rows)
                positive = sum(isinstance(row, dict) and isinstance(row.get('bid_amount_yuan'), (int, float)) and row['bid_amount_yuan'] > 0 for row in rows.values())
                add('PASS' if total and new == total else 'FAIL' if total else 'UNKNOWN', f'{tag} schema', f'new={new}/{total} ({100*new/total:.1f}%)' + (f' bid_positive={positive}' if tag == '0925' else ''))
            try:
                view = read_auction_view(GetOnly(client), day, tag)
                add('PASS', f'{tag} core', f'rows={len(view)}')
            except ViewError as exc:
                add('FAIL', f'{tag} core', str(exc))
        except Exception as exc:
            del results[start:]
            add('UNKNOWN', f'{tag} publication', f'read error {type(exc).__name__}')
            add('UNKNOWN', f'{tag} schema', 'publication unavailable')
            add('UNKNOWN', f'{tag} core', 'publication unavailable')
    for cutoff in ('093200', '093500', '094000', '094530'):
        try:
            blob = client.get(f'core:cache:states_at:{day}:{cutoff}') if client else None
            if blob is None:
                add('UNKNOWN', f'cache {cutoff}', 'missing or Redis unavailable')
            else:
                version = json.loads(gzip.decompress(blob)).get('algorithm_version')
                add('PASS' if version == 'states-at-v3' else 'FAIL', f'cache {cutoff}', f'algorithm_version={version}')
        except Exception as exc:
            add('UNKNOWN', f'cache {cutoff}', f'read error {type(exc).__name__}')
    try:
        directory = base / f'reports/{day}/auction_0926'
        summary = report_summary(json.loads((directory / 'manifest.json').read_text()), json.loads((directory / 'report.json').read_text()))
        ok = summary['seal_label'] is not None and summary['counts'] is not None and all(v is not None for v in summary['counts'].values()) and all(summary['sources'].values())
        add('PASS' if ok and all(v == 'redis_auction_full' for v in summary['sources'].values()) else 'FAIL' if ok else 'UNKNOWN', 'report', f"seal_label={summary['seal_label']} counts={summary['counts']} sources={summary['sources']}")
    except (OSError, ValueError, TypeError, AttributeError):
        add('UNKNOWN', 'report', 'manifest or report unavailable/invalid')
    for label, path, word in [('DENY', Path('/home/exedev/fw/retros/guard.log'), 'DENY'), ('BLOCK', Path('/home/exedev/fw/guard/blocked.log'), 'BLOCK')]:
        try:
            count = sum(day in line and word in line for line in path.open(encoding='utf-8', errors='replace'))
            add('PASS' if count == 0 else 'FAIL', label, f'{count} lines')
        except OSError:
            add('UNKNOWN', label, 'log unavailable')
    processes = []
    try:
        uptime = float(Path('/proc/uptime').read_text().split()[0]); ticks = os.sysconf('SC_CLK_TCK')
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():
                continue
            try:
                if (proc / 'comm').read_text().strip() != 't1_v2':
                    continue
                exe = os.readlink(proc / 'exe')
                stat = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
                started = datetime.now(timezone(timedelta(hours=8))) - timedelta(seconds=uptime - int(stat[19]) / ticks)
                processes.append((proc.name, exe, started.isoformat(timespec='seconds')))
            except (OSError, ValueError, IndexError):
                continue
        add('PASS' if processes and all(x[1] == TARGET_EXE for x in processes) else 'FAIL' if processes else 'UNKNOWN', 't1_v2', '; '.join(f'pid={pid} exe={exe} started={start}' for pid, exe, start in processes) or 'no readable process')
    except OSError:
        add('UNKNOWN', 't1_v2', 'process information unavailable')
    for status, name, detail in results:
        print(f'{status} {name}: {detail}')
    counts = Counter(status for status, _, _ in results)
    print(f"汇总: {counts['PASS']} PASS / {counts['FAIL']} FAIL / {counts['UNKNOWN']} UNKNOWN")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', required=True, type=lambda value: datetime.strptime(value, '%Y-%m-%d').date().isoformat())
    return run(parser.parse_args(argv).date)


if __name__ == '__main__':
    raise SystemExit(main())
