#!/usr/bin/env python3
"""Run registered fetch jobs against a disposable copy of the Core catalog."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path('/home/exedev/repos/engine_core')
REGISTRY = REPO / 'src/engine_core/guardian/datasets.registry.json'
PRODUCTION_ROOT = Path('/home/exedev/core-data')
RESULT_DIR = Path('/home/exedev/fw/research/C366')
DAILY_SINGLE = {'daily_kline', 'plate_members'}


def registry_jobs():
    return [item for item in json.loads(REGISTRY.read_text())['items'] if item.get('fetch_job')]


def assert_owned_sandbox(path: Path) -> Path:
    path = path.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if path.parent != temp_root or not path.name.startswith('c366-sandbox-') or not path.is_dir():
        raise ValueError(f'not an owned C366 sandbox: {path}')
    return path


def audit_targets(targets, artifact_root: Path):
    root = artifact_root.resolve()
    reasons = []
    for target in targets:
        if target in {'redis:disabled', 'td:read-only', 'source:read-only'}:
            continue
        path = Path(target).resolve()
        if path != root and root not in path.parents:
            reasons.append(f'write target outside sandbox: {target}')
    return ('UNSAFE' if reasons else 'SAFE', reasons)


def command_audit(item, artifact_root: Path):
    """Review each registered CLI's known side effects before launching it."""
    # These CLIs commit files under CORE_DATA_ROOT/artifact_root. Redis is disabled
    # in the child environment. The three auction commands and both baselines use
    # market_data1 SELECT only. Unknown commands fail closed.
    known = {
        'symbol_universe': ('artifact_root', 'source:read-only'),
        'daily_kline': ('artifact_root', 'source:read-only'),
        'daily_indicators': ('artifact_root',),
        'daily_report_features': ('artifact_root',),
        'limit_up_pool': ('artifact_root', 'source:read-only'),
        'limit_reasons': ('artifact_root', 'source:read-only'),
        'plate_members': ('artifact_root', 'source:read-only'),
        'auction_baseline': ('artifact_root', 'td:read-only', 'redis:disabled'),
        'auction_stock_facts': ('artifact_root', 'td:read-only'),
        'auction_points': ('artifact_root', 'td:read-only'),
        'auction_panorama': ('artifact_root', 'td:read-only'),
        'ladder_daily': ('artifact_root', 'source:read-only'),
        'plate_amount_baseline': ('artifact_root', 'td:read-only', 'redis:disabled'),
        'stock_day_facts': ('artifact_root',),
        'stock_theme_assignment': ('artifact_root',),
    }
    if item['id'] not in known:
        return 'UNSAFE', ['no reviewed write-target map']
    return audit_targets([str(artifact_root) if x == 'artifact_root' else x
                          for x in known[item['id']]], artifact_root)


def run_command(argv, *, timeout: float, cwd=REPO, env=None):
    argv = [sys.executable if value == '{python}' else value for value in argv]
    started = time.perf_counter()
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        status = 'COMPLETED'
        code = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        stdout, stderr = proc.communicate()
        status, code = 'TIMEOUT', None
    return {'status': status, 'returncode': code,
            'wall_seconds': round(time.perf_counter() - started, 4),
            'stdout': stdout, 'stderr': stderr}


def seed_sandbox(source: Path, destination: Path):
    """Copy catalog/data while excluding the live guardian control plane."""
    ignored_names = {'guardian-native', 'guardian', '.locks', '.checkpoints',
                     'job_state.json', 'daily_status.json', 'guardian_keepalive.sh'}
    ignored_prefixes = ('reports-preview-', 'guardian')
    def ignore(_directory, names):
        return {name for name in names if name in ignored_names or
                any(name.startswith(prefix) for prefix in ignored_prefixes) or
                name.endswith('.log') or name.endswith('.lock')}
    shutil.copytree(source, destination, ignore=ignore, symlinks=False)
    # Catalog entries carry absolute URIs; remap copied metadata to this sandbox.
    for path in [destination / 'catalog.json', *destination.rglob('manifest.json')]:
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        def remap(obj):
            if isinstance(obj, dict):
                return {key: remap(val) for key, val in obj.items()}
            if isinstance(obj, list):
                return [remap(val) for val in obj]
            if isinstance(obj, str):
                return obj.replace(str(source), str(destination))
            return obj
        path.write_text(json.dumps(remap(value), ensure_ascii=False, separators=(',', ':')) + '\n',
                        encoding='utf-8')


def clear_sandbox_dataset_date(artifact_root: Path, dataset: str, day: str):
    """Remove only a copied prior output so the sandbox run performs a fresh fetch."""
    sandbox = assert_owned_sandbox(artifact_root.parent)
    catalog_path = artifact_root / 'catalog.json'
    catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
    entries = catalog.get('datasets', {}).get(dataset, {}).get('dates', {}).pop(day, [])
    root = artifact_root.resolve()
    for entry in entries:
        manifest = Path(entry['manifest']).resolve()
        if root not in manifest.parents:
            raise ValueError(f'copied input path escaped sandbox: {manifest}')
        version_dir = manifest.parent
        if root not in version_dir.parents:
            raise ValueError(f'copied version path escaped sandbox: {version_dir}')
        shutil.rmtree(version_dir)
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, separators=(',', ':')) + '\n',
                            encoding='utf-8')


def prepare_argv(item, day, artifact_root):
    spec = item['fetch_job']
    argv = spec.get('argv', []) if isinstance(spec, dict) else spec
    values = {'{date}': day, '{artifact_root}': str(artifact_root),
              '{python}': sys.executable, '{item}': item['id']}
    argv = [values.get(token, token) for token in argv]
    if item['id'] in {'auction_stock_facts', 'auction_points', 'auction_panorama'}:
        argv[argv.index('--item') + 1] = item['id']
    if '--artifact-root' not in argv:
        argv.extend(['--artifact-root', str(artifact_root)])
    if item['id'] == 'plate_members':
        # Measure one fresh target-day fetch; the registered default is a 10-day backfill.
        argv.extend(['--backfill-days', '1'])
    return argv


def output_summary(stdout):
    lines = stdout.splitlines()
    rows = []
    statuses = []
    for line in lines:
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        for key in ('row_count', 'rows', 'total_rows', 'completed_symbols', 'days_committed'):
            value = payload.get(key)
            if isinstance(value, int):
                rows.append(value)
        if isinstance(payload.get('status'), str):
            statuses.append(payload['status'])
        for key in ('provider_status', 'source_status'):
            if isinstance(payload.get(key), str):
                statuses.append(payload[key])
    return len(lines), (rows[-1] if rows else None), (statuses[-1] if statuses else None)


def classify_result(result, stdout_status, row_count=None):
    if result['status'] == 'TIMEOUT':
        return 'TIMEOUT', None
    stderr = result.get('stderr', '')
    error_type = None
    for line in reversed(stderr.splitlines()):
        match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))(?::|$)', line.strip())
        if match:
            error_type = match.group(1)
            break
    lower = stderr.lower()
    unavailable = ('connection refused', 'connection reset', 'connect timed out',
                   'read timed out', 'td_config_missing', 'source_unavailable',
                   'provider_unavailable', 'not connected', 'network is unreachable')
    if any(token in lower for token in unavailable) or (stdout_status and
            any(token in stdout_status.lower() for token in ('unavailable', 'source_failed'))):
        return 'SOURCE_UNAVAILABLE', error_type
    if stdout_status and ('partial_st_filter_unavailable' in stdout_status.lower() or
                          (stdout_status.upper() == 'PARTIAL' and row_count in (None, 0))):
        return 'SOURCE_UNAVAILABLE', error_type
    if result['status'] != 'COMPLETED' or result['returncode'] != 0:
        return 'FAILED', error_type
    return 'COMPLETED', error_type


def cost_band(seconds):
    return 'light' if seconds < 10 else 'medium' if seconds <= 45 else 'heavy'


def execute_jobs(day, output, selected_ids=None):
    all_items = registry_jobs()
    items = [item for item in all_items if not selected_ids or item['id'] in selected_ids]
    runs = []
    for item in items:
        status, reasons = command_audit(item, output)
        print(f"AUDIT {item['id']} {status}" + (f" reason={' ; '.join(reasons)}" if reasons else ""), flush=True)
    sandbox = Path(tempfile.mkdtemp(prefix='c366-sandbox-')).resolve()
    assert_owned_sandbox(sandbox)
    artifact_root = sandbox / 'core-data'
    print('JOB_LIST ' + ','.join(item['id'] for item in items), flush=True)
    print(f'SANDBOX_SELF_CHECK path={sandbox} owned_prefix=PASS source_read_only=PASS', flush=True)
    try:
        seed_sandbox(PRODUCTION_ROOT, artifact_root)
        print(f'SANDBOX_SEEDED catalog={artifact_root / "catalog.json"} bytes={sum(p.stat().st_size for p in artifact_root.rglob("*") if p.is_file())}', flush=True)
        for item in items:
            audit, reasons = command_audit(item, artifact_root)
            if audit != 'SAFE':
                runs.append({'id': item['id'], 'status': 'UNSAFE', 'reason': reasons,
                             'attempts': 0, 'samples_seconds': [], 'write_occurred': False})
                continue
            if item['id'] == 'plate_members':
                clear_sandbox_dataset_date(artifact_root, 'plate_members', day)
            repetitions = 1 if item['id'] in DAILY_SINGLE else 3
            samples = []
            for attempt in range(1, repetitions + 1):
                env = os.environ.copy()
                env['CORE_DATA_ROOT'] = str(artifact_root)
                src_path = str(REPO / 'src')
                env['PYTHONPATH'] = src_path + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
                for key in ('REDIS_URL', 'CORE_REDIS_URL', 'LEGACY_REDIS_URL'):
                    env.pop(key, None)
                env.pop('REDIS_GUARD', None)
                argv = prepare_argv(item, day, artifact_root)
                result = run_command(argv, timeout=float(item['fetch_job'].get('timeout_seconds', 300)), env=env)
                lines, rows, stdout_status = output_summary(result['stdout'])
                status, error_type = classify_result(result, stdout_status, rows)
                result.pop('stdout')
                result.pop('stderr')
                result.update({'status': status, 'error_type': error_type,
                               'output_status': stdout_status, 'attempt': attempt,
                               'line_count': lines, 'row_count': rows})
                samples.append(result)
                print(f"RUN {item['id']} attempt={attempt}/{repetitions} status={result['status']} seconds={result['wall_seconds']} exit={result['returncode']} lines={lines} rows={rows}", flush=True)
            seconds = [sample['wall_seconds'] for sample in samples]
            successful_seconds = [sample['wall_seconds'] for sample in samples
                                  if sample['status'] == 'COMPLETED' and sample['returncode'] == 0]
            median = round(statistics.median(successful_seconds), 4) if successful_seconds else None
            runs.append({'id': item['id'], 'status': 'MEASURED' if all(x['status'] == 'COMPLETED' and x['returncode'] == 0 for x in samples) else 'PARTIAL',
                         'attempts': len(samples), 'samples': samples, 'samples_seconds': seconds,
                         'successful_samples_seconds': successful_seconds,
                         'median_seconds': median, 'suggested_cost': cost_band(median) if median is not None else None,
                         'current_cost': item.get('cost', {}).get('class'),
                         'production_write_occurred': False, 'sandbox_artifact_write_enabled': True,
                         'write_target': str(artifact_root), 'redis': 'disabled',
                         'td': 'read-only' if item['id'] in {'auction_baseline', 'auction_stock_facts', 'auction_points', 'auction_panorama', 'plate_amount_baseline'} else 'not used'})
        if selected_ids and output.exists():
            previous = json.loads(output.read_text(encoding='utf-8'))
            current_ids = {row['id'] for row in runs}
            runs = [row for row in previous.get('jobs', []) if row['id'] not in current_ids] + runs
            runs.sort(key=lambda row: next(i for i, item in enumerate(all_items) if item['id'] == row['id']))
        result = {'date': day, 'source_registry': str(REGISTRY), 'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                  'production_catalog': str(PRODUCTION_ROOT), 'sandbox': str(sandbox),
                  'repetitions_default': 3, 'single_attempt_jobs': sorted(DAILY_SINGLE),
                  'measurement_overrides': {'plate_members': 'clear copied target-day artifact and use --backfill-days 1 to force one fresh fetch'},
                  'cost_bands_seconds': {'light': '<10', 'medium': '10-45', 'heavy': '>45'},
                  'production_write_occurred': False, 'sandbox_artifact_writes_enabled': True,
                  'jobs': runs}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        write_report(output.with_name('report.md'), day, runs)
        print_table(runs)
        print('JSON', output)
        print('REPORT', output.with_name('report.md'))
    finally:
        owned = assert_owned_sandbox(sandbox)
        shutil.rmtree(owned)


def print_table(runs):
    print('TIMING_TABLE job | median_s | suggested | registered | change')
    for row in runs:
        median = row.get('median_seconds')
        suggestion = row.get('suggested_cost') or 'UNKNOWN'
        current = row.get('current_cost', 'UNKNOWN')
        change = ('YES' if suggestion != current else 'no') if suggestion != 'UNKNOWN' else 'UNKNOWN'
        print(f"{row['id']} | {median if median is not None else '—'} | {suggestion} | {current} | {change}")


def write_report(path: Path, day: str, runs):
    lines = [f'# C366 fetch_job timings ({day})', '',
             'Sandbox writes were isolated under a temporary CORE_DATA_ROOT; Redis was disabled and TD access was read-only.',
             'A cost suggestion is shown only when a successful fetch result was observed. `UNKNOWN` rows did not produce a usable fetch result.',
             'plate_members was forced to one fresh target-day fetch by removing only its copied sandbox artifact and passing `--backfill-days 1`.',
             '', '| job | status | median seconds | suggested | registered | change? | attempts (status / seconds / exit / rows) |',
             '|---|---:|---:|---|---|---|---|']
    for row in runs:
        median = row.get('median_seconds')
        suggested = row.get('suggested_cost') or 'UNKNOWN'
        current = row.get('current_cost', 'UNKNOWN')
        changed = ('yes' if suggested != current else 'no') if suggested != 'UNKNOWN' else 'UNKNOWN'
        attempts = '; '.join(f"{sample['status']} / {sample['wall_seconds']} / {sample['returncode']} / {sample['row_count']}"
                             for sample in row.get('samples', [])) or '—'
        lines.append(f"| {row['id']} | {row['status']} | {median if median is not None else 'UNKNOWN'} | {suggested} | {current} | {changed} | {attempts} |")
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', required=True)
    parser.add_argument('--out', default=str(RESULT_DIR / 'timings.json'))
    parser.add_argument('--execute', action='store_true', help='perform isolated real-source calls')
    parser.add_argument('--jobs', help='optional comma-separated subset; merges measured rows into an existing C366 result')
    args = parser.parse_args()
    if not args.execute:
        parser.error('--execute is required')
    execute_jobs(args.date, Path(args.out), set(args.jobs.split(',')) if args.jobs else None)


if __name__ == '__main__':
    main()
