"""Resolve a restorable hot view without confusing unavailable stores with absence."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

_LOG = logging.getLogger(__name__)


class Presence(str):
    def __new__(cls, status, reason=None):
        value = super().__new__(cls, status)
        value.reason = reason
        return value


def _validate_pool(raw, trade_date, pool):
    try:
        value = pool._validate(json.loads(raw), trade_date)
        pool._result(value, 'redis', None, datetime.now(timezone.utc).isoformat(), False, None)
        return value
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        from ..data_pipeline.cold_archive import CorruptArchive
        raise CorruptArchive(str(exc) or type(exc).__name__) from exc


def resolve_presence(dataset, trade_date, *, redis_client=None, cold_store=None):
    if dataset == 'limit_up_pool_today':
        from ..data_pipeline.reader import _read_today_pool
        result = _read_today_pool(trade_date, client=redis_client, cold_store=cold_store)
        if result.status == 'READY':
            return 'RESTORED_FROM_TD' if result.discovered_by == 'td' else 'PRESENT_REDIS'
        return Presence(result.status, result.manifest.get('reason'))
    if dataset != 'limit_up_pool':
        raise ValueError('unsupported_presence_dataset')
    from .registry import load_registry
    item = next(item for item in load_registry() if item['id'] == dataset)
    cold_dataset = item['cold_store']['dataset']
    from ..data_pipeline import cold_archive, limit_pool_hot_view as pool
    from ..data_pipeline.redis_day_view import hot_view_expire_at

    store = None
    try:
        client = redis_client if redis_client is not None else pool._client()
        key = item['hot_store']['key'].format(data_date=trade_date, trade_date=trade_date)
        raw = client.get(key)
        if raw is not None:
            _validate_pool(raw, trade_date, pool)
            return 'PRESENT_REDIS'
        if cold_store is None:
            if not cold_archive._enabled():
                raise RuntimeError('cold_archive_disabled')
            store = cold_archive.TdStore()
        else:
            store = cold_store
        version = store.latest_version(cold_dataset, trade_date)
        if version is None:
            return 'MISSING'
        raw = cold_archive.read_envelope(cold_dataset, trade_date, version, store=store)
        value = _validate_pool(raw, trade_date, pool)
        payload_version = value['version']
        hashed_version = payload_version + ':' + hashlib.sha256(raw).hexdigest()[:16]
        if version not in (payload_version, hashed_version):
            raise cold_archive.CorruptArchive('cold_archive_version_mismatch')
        if not client.set(key, raw, exat=hot_view_expire_at(trade_date), nx=True):
            current = client.get(key)
            _validate_pool(current, trade_date, pool)
            return 'PRESENT_REDIS'
        return 'RESTORED_FROM_TD'
    except cold_archive.CorruptArchive as exc:
        reason = str(exc) or type(exc).__name__
        _LOG.warning('presence_corrupt dataset=%s trade_date=%s reason=%s', dataset, trade_date, reason)
        return Presence('CORRUPT', reason)
    except Exception as exc:
        _LOG.warning('presence_unknown dataset=%s trade_date=%s error=%s', dataset, trade_date, type(exc).__name__)
        return 'UNKNOWN'
    finally:
        if store is not None and cold_store is None:
            try:
                store.close()
            except Exception:
                _LOG.warning('cold_archive_close_failed', exc_info=True)
