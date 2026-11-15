"""Resolve a restorable hot view without confusing unavailable stores with absence."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

_LOG = logging.getLogger(__name__)


def resolve_presence(dataset, trade_date, *, redis_client=None, cold_store=None):
    if dataset != 'limit_up_pool':
        raise ValueError('unsupported_presence_dataset')
    from ..data_pipeline import cold_archive, limit_pool_hot_view as pool
    from ..data_pipeline.redis_day_view import hot_view_expire_at

    store = None
    try:
        client = redis_client if redis_client is not None else pool._client()
        key = pool._key(trade_date, 'core:view:limit_up_pool')
        raw = client.get(key)
        if raw is not None:
            value = pool._validate(json.loads(raw), trade_date)
            pool._result(value, 'redis', None, datetime.now(timezone.utc).isoformat(), False, None)
            return 'PRESENT_REDIS'
        if cold_store is None:
            if not cold_archive._enabled():
                raise RuntimeError('cold_archive_disabled')
            store = cold_archive.TdStore()
        else:
            store = cold_store
        version = store.latest_version(dataset, trade_date)
        if version is None:
            return 'MISSING'
        raw = cold_archive.read_envelope(dataset, trade_date, version, store=store)
        value = pool._validate(json.loads(raw), trade_date)
        payload_version = value['version']
        hashed_version = payload_version + ':' + hashlib.sha256(raw).hexdigest()[:16]
        if version not in (payload_version, hashed_version):
            raise ValueError('cold_archive_version_mismatch')
        pool._result(value, 'redis', None, datetime.now(timezone.utc).isoformat(), False, None)
        if not client.set(key, raw, exat=hot_view_expire_at(trade_date), nx=True):
            current = client.get(key)
            value = pool._validate(json.loads(current), trade_date)
            pool._result(value, 'redis', None, datetime.now(timezone.utc).isoformat(), False, None)
            return 'PRESENT_REDIS'
        return 'RESTORED_FROM_TD'
    except Exception as exc:
        _LOG.warning('presence_unknown dataset=%s trade_date=%s error=%s', dataset, trade_date, type(exc).__name__)
        return 'UNKNOWN'
    finally:
        if store is not None and cold_store is None:
            try:
                store.close()
            except Exception:
                _LOG.warning('cold_archive_close_failed', exc_info=True)
