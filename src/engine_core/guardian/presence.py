"""Resolve a restorable hot view without confusing unavailable stores with absence."""
from __future__ import annotations

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
    if dataset == 'hot_rank':
        from ..data_pipeline.limit_pool_hot_view import _client
        from ..storage.two_layer import TwoLayerStore
        client = redis_client
        try:
            if client is None:
                client = _client()
            result = TwoLayerStore(client, cold_store=cold_store).presence(dataset, trade_date)
            return Presence(result.status, result.reason)
        except Exception:
            return Presence('UNKNOWN', 'read_failed')
        finally:
            if redis_client is None and client is not None:
                client.close()
    if dataset == 'limit_up_pool_today':
        from ..data_pipeline.reader import _read_today_pool
        result = _read_today_pool(trade_date, client=redis_client, cold_store=cold_store)
        if result.status == 'READY':
            return 'PRESENT_REDIS'
        return Presence(result.status, result.manifest.get('reason'))
    if dataset != 'limit_up_pool':
        raise ValueError('unsupported_presence_dataset')
    from ..storage.two_layer import TwoLayerStore
    from ..data_pipeline import limit_pool_hot_view as pool
    client = redis_client
    try:
        if client is None:
            client = pool._client()
        result = TwoLayerStore(client, cold_store).get(dataset, trade_date)
        if result.metadata.get('fallback_reason') == 'redis_corrupt':
            reason = 'redis_corrupt'
            if result.metadata.get('cold_recoverable'):
                reason += ';cold_' + result.metadata['cold_status'].lower() + ';cold_recoverable'
            return Presence('CORRUPT', reason)
        if (result.metadata.get('fallback_reason') or '').startswith('redis_') and result.metadata.get('fallback_reason') != 'redis_missing':
            return Presence('UNKNOWN', result.reason or result.metadata['fallback_reason'])
        if result.status == 'UNKNOWN' and any(f.get('reason') == 'cold_missing' for f in result.metadata.get('selection_failures', [])):
            return Presence('MISSING', 'cold_missing')
        return Presence(result.status, result.reason)
    except Exception:
        return Presence('UNKNOWN', 'read_failed')
    finally:
        if redis_client is None and client is not None:
            try: client.close()
            except Exception: pass
