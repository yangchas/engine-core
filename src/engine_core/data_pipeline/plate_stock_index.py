"""Point-in-time inverse index of reference plate memberships."""
from __future__ import annotations

from datetime import date, datetime
import re
from zoneinfo import ZoneInfo


def build_index(rows):
    index = {}
    seen = set()
    for row in rows:
        symbol, code = row['symbol'], row['plate_code']
        if not isinstance(symbol, str) or not re.fullmatch(r'[0-9]{6}', symbol) or not isinstance(code, str) or not code:
            raise ValueError('invalid_plate_members_identity')
        if (symbol, code) in seen:
            raise ValueError('duplicate_plate_members_identity')
        seen.add((symbol, code))
        index.setdefault(symbol, []).append({key: row[key] for key in
                                            ('plate_code', 'plate_class', 'plate_member_label')})
    for plates in index.values():
        plates.sort(key=lambda item: item['plate_code'])
    return dict(sorted(index.items()))


def run(trade_date, asof, *, reference_store=None, store=None, redis_client=None):
    from .reference_snapshot_td import ReferenceSnapshotStore
    from .auction_redis_view import redis_client as make_redis
    from ..storage.two_layer import TwoLayerStore
    day = date.fromisoformat(trade_date).isoformat()
    cutoff = datetime.fromisoformat(asof.replace('Z', '+00:00'))
    if cutoff.tzinfo is None or cutoff.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat() != day:
        raise ValueError('invalid_asof')
    cutoff = cutoff.astimezone(ZoneInfo('Asia/Shanghai')).isoformat()
    ref = reference_store or ReferenceSnapshotStore()
    try:
        snapshot = ref.read_as_of('plate_members', cutoff)
    finally:
        if reference_store is None:
            ref.close()
    if not snapshot:
        raise ValueError('plate_members_unavailable')
    available = datetime.fromisoformat(snapshot['available_at'].replace('Z', '+00:00'))
    if available.tzinfo is None or available > datetime.fromisoformat(cutoff):
        raise ValueError('plate_members_not_visible')
    index = build_index(snapshot['rows'])
    rows = [{'symbol': symbol, 'plates': plates} for symbol, plates in index.items()]
    instant = datetime.fromisoformat(cutoff)
    epoch = datetime(1970, 1, 1, tzinfo=ZoneInfo('UTC'))
    delta = instant - epoch
    asof_id = format((delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds, 'x')
    metadata = {'version': snapshot['version'] + ':' + asof_id, 'member_version': snapshot['version'],
                'member_available_at': available.astimezone(ZoneInfo('Asia/Shanghai')).isoformat(),
                'available_at': cutoff, 'asof': cutoff}
    return (store or TwoLayerStore(redis_client or make_redis())).publish('plate_stock_index', day, rows, metadata)


def get_stock_plates(symbols, trade_date, *, store=None, redis_client=None):
    from .auction_redis_view import redis_client as make_redis
    from ..storage.two_layer import TwoLayerStore
    return (store or TwoLayerStore(redis_client or make_redis())).get_stock_plates(trade_date, symbols)


def _json(value):
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _envelope(day, rows, metadata):
    import hashlib
    payload = {'dataset': 'plate_stock_index', 'trade_date': day, 'rows': rows,
               'row_count': len(rows), **metadata}
    raw = _json(payload).encode('utf-8')
    version = metadata['version'] + ':' + hashlib.sha256(raw).hexdigest()[:16]
    if len(version) > 64:
        raise ValueError('archive_version_too_long')
    return payload, raw, version


def _write_view(redis, day, value):
    from .redis_day_view import write_day_view
    rows = {row['symbol']: {'plates': row['plates'], 'member_version': value['member_version']}
            for row in value['rows']}
    if len(rows) != value['row_count']:
        raise ValueError('duplicate_index_symbol')
    write_day_view(redis, 'plate_stock_index', day, rows, source='plate_members',
                   version=value['version'], available_at=value['available_at'],
                   fields=('plates', 'member_version'))


def publish(store, day, rows, metadata):
    from ..storage.two_layer import StoreResult
    from . import cold_archive
    try:
        value, raw, version = _envelope(day, rows, metadata)
        receipt = cold_archive.archive('plate_stock_index', day, version, raw, value)
        if receipt is False or receipt is None:
            return StoreResult(status='FAILED', version=version, archive_status='NOT_QUEUED', reason='cold_archive_not_queued')
        if not receipt.done.wait(6):
            return StoreResult(status='FAILED', version=version, archive_status='UNKNOWN', reason='cold_archive_timeout')
        if not receipt.success:
            return StoreResult(status='FAILED', version=version, archive_status='FAILED', reason=receipt.error_category)
        _write_view(store.redis, day, value)
        return StoreResult(status='published', version=value['version'], metadata=value, archive_status='ARCHIVED')
    except Exception as exc:
        return StoreResult(status='FAILED', reason=type(exc).__name__)


def read_stocks(store, day, symbols):
    import json
    from . import cold_archive
    from .redis_day_view_read import read_day_view
    date.fromisoformat(day)
    symbols = list(dict.fromkeys(symbols))
    if any(not isinstance(s, str) or not re.fullmatch(r'[0-9]{6}', s) for s in symbols):
        raise ValueError('invalid_symbol')
    if not symbols:
        return {}

    class PipelinedRedis:
        def get(self, key):
            return store.redis.get(key)
        def hmget(self, key, batch):
            pipe = store.redis.pipeline()
            pipe.hmget(key, batch)
            return pipe.execute()[0]

    cold = None
    def load_cold():
        nonlocal cold
        cold = store.cold
        if cold is None:
            if not cold_archive._enabled():
                raise RuntimeError('cold_archive_disabled')
            cold = cold_archive.TdStore()
        version = cold.latest_version('plate_stock_index', day)
        if version is None:
            return None
        raw = cold_archive.read_envelope('plate_stock_index', day, version, store=cold)
        value = json.loads(raw)
        expected, canonical, computed = _envelope(day, value['rows'], {k: value[k] for k in
            ('version', 'member_version', 'member_available_at', 'available_at', 'asof')})
        if value != expected or canonical != raw or computed != version:
            raise ValueError('cold_index_integrity')
        return value

    try:
        try:
            view = read_day_view(PipelinedRedis(), 'plate_stock_index', day, symbols,
                                 td_loader=lambda *_: {}, ledger=lambda *_: None)
        except Exception:
            view = {'source': 'td_fallback', 'rows': {}, 'meta': {}}
        meta = view['meta'] if view['source'] == 'redis' else {}
        hot_rows = view['rows']
        if meta and len(hot_rows) == len(symbols):
            return {s: {'status': 'PRESENT_REDIS', 'plates': hot_rows[s]['plates'],
                        'version': meta['version']} for s in symbols}
        value = load_cold()
        if value is None:
            if meta:
                return {s: ({'status': 'PRESENT_REDIS', 'plates': hot_rows[s]['plates'], 'version': meta['version']}
                            if s in hot_rows else {'status': 'UNKNOWN', 'version': meta['version'],
                                                   'reason': 'cold_index_unavailable'}) for s in symbols}
            return {s: {'status': 'MISSING', 'version': None} for s in symbols}
        if meta and meta['version'] != value['version']:
            raise ValueError('cold_index_version_mismatch')
        index = {r['symbol']: r['plates'] for r in value['rows']}
        try:
            _write_view(store.redis, day, value)
        except Exception:
            pass
        return {s: ({'status': 'RESTORED_FROM_TD', 'plates': index[s], 'version': value['version']} if s in index
                    else {'status': 'MISSING', 'version': value['version']}) for s in symbols}
    except Exception as exc:
        if cold_archive.is_missing_table_error(exc):
            return {s: {'status': 'MISSING', 'version': None} for s in symbols}
        return {s: {'status': 'UNKNOWN', 'version': None, 'reason': type(exc).__name__} for s in symbols}
    finally:
        if cold is not None and store.cold is None:
            cold.close()
