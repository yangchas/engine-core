"""Atomic Redis view and point-in-time TD fallback for the previous-day pool.

The view digest is SHA-256 of UTF-8 canonical JSON of rows sorted by symbol,
with sorted object keys, no whitespace, and JSON null preserved. It is distinct
from the source partition's sha256. Empty pools are not a supported source result.
"""
from __future__ import annotations

import fcntl
import hashlib
from contextlib import contextmanager
from pathlib import Path
import json
import os
import re
from datetime import datetime, time, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .limit_pool import DATASET, FIELD_UNITS, PROVIDER_RANK, REQUIRED_COLUMNS, SCHEMA_VERSION
from .trade_calendar import nth_trade_date
from .time_format import shanghai_seconds_ceil


def _instant(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def _canonical(rows):
    return json.dumps(sorted(rows, key=lambda r: r['symbol']), ensure_ascii=False,
                      sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _digest(rows):
    return hashlib.sha256(_canonical(rows)).hexdigest()


def _key(day, prefix):
    datetime.strptime(day, '%Y-%m-%d')
    if prefix != 'core:view:limit_up_pool' and not prefix.startswith('core:test:'):
        raise ValueError('invalid_pool_prefix')
    return f'{prefix}:{day}'


def _validate(value, day):
    if not isinstance(value, dict) or value.get('dataset') != DATASET or value.get('schema_version') != SCHEMA_VERSION or value.get('trade_date') != day or value.get('state', 'COMPLETE') != 'COMPLETE':
        raise ValueError('invalid_envelope')
    rows = value.get('rows')
    quality = value.get('quality')
    if not isinstance(rows, list) or not rows or type(value.get('row_count')) is not int or value['row_count'] != len(rows):
        raise ValueError('invalid_row_count')
    if not isinstance(quality, dict) or quality.get('status') not in {'READY', 'PARTIAL', 'DEGRADED'} or value.get('business_status') not in {'FORMAL', 'PROVISIONAL', 'DEGRADED', 'PARTIAL'} or type(value.get('degraded')) is not bool:
        raise ValueError('invalid_quality')
    if value.get('provider') not in PROVIDER_RANK or not value.get('source') or not value.get('version') or _instant(value.get('available_at')) is None:
        raise ValueError('invalid_source_metadata')
    symbols = set()
    for row in rows:
        if not isinstance(row, dict) or any(key not in row for key in REQUIRED_COLUMNS):
            raise ValueError('missing_required_field')
        symbol = row['symbol']
        if not isinstance(symbol, str) or not re.fullmatch(r'\d{6}', symbol) or symbol in symbols:
            raise ValueError('invalid_symbol')
        if row['date'] != day or row['trade_date'] != day or not isinstance(row['name'], str) or not row['name'] or type(row['lb_days']) is not int or row['lb_days'] < 1 or row['in_legacy_pool'] is not True:
            raise ValueError('invalid_row')
        symbols.add(symbol)
    if value.get('rows_sha256') != _digest(rows):
        raise ValueError('digest_mismatch')
    return value


def _client():
    url = os.environ.get('CORE_REDIS_URL') or os.environ.get('REDIS_URL')
    if not url:
        raise RuntimeError('redis_not_configured')
    import redis
    return redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)


def _td_store():
    if not os.environ.get('TD_CONFIG'):
        raise RuntimeError('td_not_configured')
    from ..guardian.daily_kline_coverage import td_settings
    from .limit_pool_td import PoolTaosStore
    settings = td_settings()
    if not settings:
        raise RuntimeError('td_not_configured')
    return PoolTaosStore(str(settings['database']))


def envelope(day, rows, manifest):
    if manifest.get('row_count') != len(rows):
        raise ValueError('source_row_count_mismatch')
    value = {'dataset': DATASET, 'schema_version': SCHEMA_VERSION, 'trade_date': day,
             'row_count': len(rows), 'rows': rows,
             'rows_sha256': _digest(rows), 'source_sha256': manifest.get('sha256', manifest.get('source_sha256')),
             'field_units': manifest.get('field_units', FIELD_UNITS),
             **{key: manifest.get(key) for key in ('source', 'provider', 'business_status', 'degraded',
                                                     'quality', 'version', 'available_at')}}
    value['available_at'] = shanghai_seconds_ceil(value['available_at'])
    return _validate(value, day)


@contextmanager
def pool_lock(root):
    """Serialize pool writers sharing the existing local data-root directory inode."""
    directory = Path(root).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def priority(item):
    quality = item['quality']
    formal = (item['business_status'] == 'FORMAL' and not item['degraded'] and
              quality['status'] == 'READY' and not quality.get('partial'))
    return (formal, -PROVIDER_RANK[item['provider']], _instant(item['available_at']))


def deadline_for(day, calendar_root):
    from .redis_day_view import hot_view_expire_at
    return hot_view_expire_at(day, root=calendar_root) if calendar_root is not None else hot_view_expire_at(day)


def expiry_matches(client, key, deadline, now):
    ttl = client.ttl(key)
    return ttl >= 0 and abs(ttl - (deadline - int(now.timestamp()))) <= 2


def publish(day, rows, manifest, *, redis_client=None, prefix='core:view:limit_up_pool',
            calendar_root=None, clock=None, lock_root=None, lock_held=False, fresh=False, on_write=None):
    """Publish one SET EXAT while serializing pool writers on the local data root."""
    if not lock_held:
        root = lock_root or calendar_root or os.environ.get('CORE_DATA_ROOT') or '~/core-data'
        with pool_lock(root):
            return publish(day, rows, manifest, redis_client=redis_client, prefix=prefix,
                           calendar_root=calendar_root, clock=clock, lock_held=True, fresh=fresh, on_write=on_write)
    client = redis_client if redis_client is not None else _client()
    key = _key(day, prefix)
    deadline = deadline_for(day, calendar_root)
    now = clock() if clock else datetime.now(ZoneInfo('Asia/Shanghai'))
    if deadline <= int(now.timestamp()):
        raise ValueError('pool_expiry_in_past')
    raw = client.get(key)
    if fresh:
        published_at = (clock() if clock else datetime.now(timezone.utc)).astimezone(timezone.utc)
        manifest = {**manifest, 'available_at': shanghai_seconds_ceil(published_at)}
    value = envelope(day, rows, manifest)
    if raw:
        try:
            old = _validate(json.loads(raw), day)
            if _instant(old['available_at']) > now:
                old = None
        except (ValueError, TypeError, UnicodeError):
            old = None
        if old:
            same = ({k: v for k, v in old.items() if k not in {'state', 'available_at'}} ==
                    {k: v for k, v in value.items() if k != 'available_at'})
            if same or priority(old) > priority(value):
                if not expiry_matches(client, key, deadline, now):
                    if not client.set(key, raw, exat=deadline):
                        raise RuntimeError('pool_set_failed')
                    if on_write is not None:
                        on_write(raw, old)
                    repaired = True
                else:
                    repaired = False
                return {'status': 'unchanged' if same else 'preserved_better', 'version': old['version'],
                        'expire_at': deadline, 'expiry_repaired': repaired}
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if not client.set(key, encoded, exat=deadline):
        raise RuntimeError('pool_set_failed')
    if on_write is not None:
        on_write(encoded, value)
    return {'status': 'published', 'version': value['version'], 'expire_at': deadline}


def _result(value, storage, reason, asof, late_rebuild, late_asof):
    available = _instant(value['available_at'])
    cutoff = _instant(asof)
    if cutoff is None:
        raise ValueError('invalid_asof')
    late = available > cutoff
    if late and (not late_rebuild or _instant(late_asof) is None or available > _instant(late_asof)):
        raise ValueError('late_visible')
    quality = value['quality']
    partial = quality.get('partial') or quality.get('status') == 'PARTIAL' or value['business_status'] == 'PARTIAL'
    degraded = storage in ('td', 'cold') or value['degraded'] or value['business_status'] in {'PROVISIONAL', 'DEGRADED'} or quality.get('status') == 'DEGRADED'
    status = 'PARTIAL' if partial or late else 'DEGRADED' if degraded else 'READY'
    meta = {key: value.get(key) for key in ('source', 'provider', 'version', 'available_at', 'quality', 'business_status', 'degraded', 'trade_date', 'row_count')}
    meta.update(status=status, storage_source=storage, fallback_reason=reason, late_visible=late)
    return value['rows'], meta


def _td_value(day, store):
    versions = store.read_versions(day)
    physical = store.read_day_rows(day)
    if not physical:
        raise ValueError('td_missing')
    row_versions = {row.get('version') for row in physical.values()}
    if len(row_versions) != 1:
        raise ValueError('td_row_version_mismatch')
    version = next(iter(row_versions))
    candidates = [m for m in versions if m.get('version') == version and m.get('trade_date') == day]
    if len(candidates) != 1:
        raise ValueError('td_version_metadata_mismatch')
    manifest = candidates[0]
    rows = [{**{k: v for k, v in row.items() if k != 'version'}, 'symbol': symbol,
             'date': day, 'trade_date': day} for symbol, row in physical.items()]
    if int(manifest.get('row_count') or 0) != len(rows):
        raise ValueError('td_row_count_mismatch')
    return envelope(day, rows, manifest)


def read(day, asof, *, redis_client=None, td_store=None, cold_store=None, prefix='core:view:limit_up_pool', late_rebuild=False, late_asof=None):
    """Select a visible pool without writing; preserve failures alongside fallback data."""
    from . import cold_archive
    if _instant(asof) is None:
        return [], {'status': 'UNKNOWN', 'reason': 'invalid_asof', 'trade_date': day}
    failures = []
    client = None
    owned_client = redis_client is None
    try:
        client = redis_client if redis_client is not None else _client()
        raw = client.get(_key(day, prefix))
        if raw is None:
            failures.append(('MISSING', 'redis_missing'))
        else:
            try:
                value = _validate(json.loads(raw), day)
                return _result(value, 'redis', None, asof, late_rebuild, late_asof)
            except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                reason = str(exc) or type(exc).__name__
                failures.append(('NOT_VISIBLE', reason) if reason == 'late_visible' else ('CORRUPT', 'redis_corrupt'))
    except Exception as exc:
        failures.append(('UNKNOWN', 'redis_' + type(exc).__name__))
    finally:
        if owned_client and client is not None:
            try: client.close()
            except Exception: pass

    def selected(value, source):
        rows, meta = _result(value, source, failures[0][1], asof, late_rebuild, late_asof)
        meta['cold_recoverable'] = True
        if failures[0] == ('CORRUPT', 'redis_corrupt'):
            meta['cold_status'] = meta['status']
            meta['status'] = 'CORRUPT'
        meta['selection_failures'] = [{'status': status, 'reason': reason} for status, reason in failures]
        meta['selection_uncertain'] = any(status in ('UNKNOWN', 'CORRUPT') for status, _ in failures)
        return rows, meta

    candidates = []
    store = None
    try:
        store = td_store if td_store is not None else _td_store()
        value = _td_value(day, store)
        _result(value, 'td', None, asof, late_rebuild, late_asof)
        candidates.append((value, 'td', None))
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) else 'td_' + type(exc).__name__
        status = ('MISSING' if reason in ('td_missing', 'td_unavailable') else 'NOT_VISIBLE' if reason == 'late_visible'
                  else 'CORRUPT' if isinstance(exc, (ValueError, TypeError, KeyError)) else 'UNKNOWN')
        failures.append((status, reason))
    finally:
        if store is not None and td_store is None:
            try: store.close()
            except Exception: pass

    store = None
    try:
        versions = []
        if cold_store is None:
            if not cold_archive._enabled():
                failures.append(('MISSING', 'cold_archive_disabled'))
            else:
                store = cold_archive.TdStore()
        else:
            store = cold_store
        if store is not None:
            if hasattr(store, 'list_versions'):
                versions = store.list_versions(DATASET, day)
            else:
                version = store.latest_version(DATASET, day)
                versions = [version] if version is not None else []
            if not versions:
                failures.append(('MISSING', 'cold_missing'))
        for version in versions:
            try:
                raw = cold_archive.read_envelope(DATASET, day, version, store=store)
                value = _validate(json.loads(raw), day)
                hashed = value['version'] + ':' + hashlib.sha256(raw).hexdigest()[:16]
                if version not in (value['version'], hashed):
                    raise cold_archive.CorruptArchive('cold_archive_version_mismatch')
                _result(value, 'cold', None, asof, late_rebuild, late_asof)
                candidates.append((value, 'cold', version))
            except Exception as exc:
                reason = str(exc) if isinstance(exc, (ValueError, TypeError, KeyError)) else 'cold_' + type(exc).__name__
                status = ('NOT_VISIBLE' if reason == 'late_visible' else
                          'CORRUPT' if isinstance(exc, (ValueError, TypeError, KeyError)) else 'UNKNOWN')
                failures.append((status, reason))
    except Exception as exc:
        reason = str(exc) if isinstance(exc, (ValueError, TypeError, KeyError)) else 'cold_' + type(exc).__name__
        status = ('NOT_VISIBLE' if reason == 'late_visible' else
                  'CORRUPT' if isinstance(exc, (ValueError, TypeError, KeyError)) else 'UNKNOWN')
        failures.append((status, reason))
    finally:
        if store is not None and cold_store is None:
            try: store.close()
            except Exception: pass
    if candidates:
        value, source, archive_version = max(candidates, key=lambda item: priority(item[0]))
        rows, meta = selected(value, source)
        if archive_version is not None:
            meta['archive_version'] = archive_version
        if meta['selection_uncertain']:
            meta['reason'] = 'selection_uncertain'
        return rows, meta
    # A concrete corruption is retained even if another source is unavailable.
    status = next((kind for kind in ('CORRUPT', 'NOT_VISIBLE', 'UNKNOWN', 'MISSING')
                   if any(item[0] == kind for item in failures)), 'UNKNOWN')
    reason = next((r for k, r in failures if k == status and r != 'redis_missing'),
                  next(r for k, r in failures if k == status))
    status_out = 'MISSING' if status == 'NOT_VISIBLE' else status
    return [], {'status': status_out, 'reason': reason, 'trade_date': day,
                'storage_source': 'td',
                'fallback_reason': failures[0][1], 'quality': {'partial': True},
                'selection_failures': [{'status': kind, 'reason': detail} for kind, detail in failures],
                'selection_uncertain': status in ('UNKNOWN', 'CORRUPT')}
