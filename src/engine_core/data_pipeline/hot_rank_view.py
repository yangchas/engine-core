"""Best-effort day snapshot for THS hot rank."""
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from . import cold_archive
from .redis_day_view import hot_view_expire_at
from .time_format import shanghai_seconds, shanghai_seconds_ceil


def _instant(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('timezone_required')
    return dt.astimezone(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def _digest(rows):
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def validate(raw, day):
    value = json.loads(raw)
    rows = value['rows']
    if (value.get('dataset') != 'hot_rank' or value.get('trade_date') != day
            or not isinstance(rows, list) or not rows or value.get('row_count') != len(rows)
            or value.get('rows_sha256') != _digest(rows) or not value.get('version')):
        raise ValueError('invalid_hot_rank_envelope')
    _instant(value['available_at'])
    return value


def publish(client, day, rows, metadata):
    rows = [dict(row) for row in rows]
    for row in rows:
        if row.get('captured_at'):
            row['captured_at'] = _instant(row['captured_at'])
    value = dict(metadata, dataset='hot_rank', trade_date=day, rows=rows,
                 row_count=len(rows), rows_sha256=_digest(rows),
                 available_at=shanghai_seconds_ceil(metadata['available_at']))
    if value.get('captured_at'):
        value['captured_at'] = _instant(value['captured_at'])
    for key in ('fetched_at', 'committed_at'):
        if value.get(key):
            value[key] = shanghai_seconds(value[key])
    if value.get('declared_available_at'):
        value['declared_available_at'] = shanghai_seconds_ceil(value['declared_available_at'])
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    validate(raw, day)
    if not client.set(f'core:view:hot_rank:{day}', raw, exat=hot_view_expire_at(day)):
        raise RuntimeError('hot_rank_view_write_failed')
    try:
        queued = cold_archive.archive('hot_rank', day, value['version'], raw.encode(), value)
    except Exception as exc:
        queued = cold_archive.ArchiveReceipt()
        queued.success = False
        queued.error_category = type(exc).__name__
    return value, queued
