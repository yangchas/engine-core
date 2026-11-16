"""Prototype for the previous-day limit pool's existing hot and cold stores.

Only limit_up_pool is supported. Version enumeration and conditional advance
(in particular daily_kline latest) need dataset-specific contracts first.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any, Protocol

from ..data_pipeline import cold_archive, limit_pool_hot_view
from ..guardian.presence import resolve_presence

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoreResult:
    status: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    version: str | None = None
    archive_status: str | None = None
    reason: str | None = None


class Store(Protocol):
    def publish(self, dataset: str, partition: str, rows: list, metadata: dict) -> StoreResult: ...
    def get(self, dataset: str, partition: str, *, asof: str | None = None, version: str | None = None) -> StoreResult: ...
    def list_versions(self, dataset: str, partition_range: Any, *, asof: str | None = None) -> Any: ...
    def presence(self, dataset: str, partition: str, *, asof: str | None = None, requirements: Any = None) -> StoreResult: ...
    def advance(self, dataset: str, partition: str, version: str, *, expected_previous: str | None = None) -> Any: ...


class _ReadOnlyRedis:
    """Allow the existing resolver to validate a cold candidate without writing it."""
    def __init__(self, client):
        self.client = client

    def get(self, key):
        return self.client.get(key)

    def set(self, key, value, **kwargs):
        return True


class TwoLayerStore:
    def __init__(self, redis_client, cold_store=None):
        self.redis = redis_client
        self.cold = cold_store

    @staticmethod
    def _dataset(dataset):
        if dataset not in ('limit_up_pool', 'hot_rank', 'auction_seal_facts', 'plate_stock_index'):
            raise NotImplementedError('only limit_up_pool is supported')

    def publish(self, dataset, partition, rows, metadata):
        self._dataset(dataset)
        if dataset == 'plate_stock_index':
            from ..data_pipeline.plate_stock_index import publish
            return publish(self, partition, rows, metadata)
        if dataset == 'auction_seal_facts':
            return self._publish_auction_seal(partition, rows, metadata)
        if dataset == 'hot_rank':
            from ..data_pipeline import hot_rank_view
            value, queued = hot_rank_view.publish(self.redis, partition, rows, metadata)
            return StoreResult(status='published', version=value['version'], metadata=value,
                               archive_status='QUEUED' if queued else 'NOT_QUEUED')
        archive_status = 'NOT_SUBMITTED'

        def archive(raw, value):
            nonlocal archive_status
            raw = raw if isinstance(raw, bytes) else raw.encode('utf-8')
            version = value['version'] + ':' + hashlib.sha256(raw).hexdigest()[:16]
            try:
                queued = cold_archive.archive(dataset, partition, version, raw, value)
            except Exception:
                queued = False
            archive_status = 'QUEUED' if queued else 'NOT_QUEUED'

        result = limit_pool_hot_view.publish(partition, rows, metadata,
                                             redis_client=self.redis, on_write=archive)
        return StoreResult(status=result['status'], version=result['version'],
                           metadata=result, archive_status=archive_status)

    def _publish_auction_seal(self, partition, rows, metadata):
        from ..data_pipeline.redis_day_view import expire_at
        value = dict(metadata, dataset='auction_seal_facts', trade_date=partition,
                     rows=rows, row_count=len(rows))
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        version = value['version'] + ':' + hashlib.sha256(raw).hexdigest()[:16]
        if not self.redis.set(f'core:view:auction_seal_facts:{partition}', raw, exat=expire_at(partition)):
            return StoreResult(status='UNKNOWN', version=version, reason='redis_write_failed')
        try:
            receipt = cold_archive.archive('auction_seal_facts', partition, version, raw, value)
            if receipt is False or receipt is None:
                return StoreResult(status='published', version=version, metadata=value,
                                   archive_status='NOT_QUEUED', reason='cold_archive_not_queued')
            if not receipt.done.wait(6):
                return StoreResult(status='published', version=version, metadata=value,
                                   archive_status='UNKNOWN', reason='cold_archive_timeout')
            if not receipt.success:
                return StoreResult(status='published', version=version, metadata=value,
                                   archive_status='FAILED', reason=receipt.error_category)
            return StoreResult(status='published', version=version, metadata=value, archive_status='ARCHIVED')
        except Exception as exc:
            return StoreResult(status='published', version=version, metadata=value,
                               archive_status='FAILED', reason=type(exc).__name__)

    def get(self, dataset, partition, *, asof=None, version=None):
        self._dataset(dataset)
        if dataset in ('auction_seal_facts', 'plate_stock_index'):
            raise NotImplementedError('dataset generic read is not implemented')
        if version is not None:
            raise NotImplementedError('exact version selection is not implemented')
        if dataset == 'hot_rank':
            return self._hot_rank_get(partition, asof=asof)
        status = resolve_presence(dataset, partition, redis_client=self.redis, cold_store=self.cold)
        if status not in ('PRESENT_REDIS', 'RESTORED_FROM_TD'):
            return StoreResult(status=status, reason=getattr(status, 'reason', None) or ('resolver did not expose cause' if status == 'UNKNOWN' else None))
        try:
            raw = self.redis.get(limit_pool_hot_view._key(partition, 'core:view:limit_up_pool'))
            from ..guardian.presence import _validate_pool
            value = _validate_pool(raw, partition, limit_pool_hot_view)
            cutoff = asof or datetime.now(timezone.utc).isoformat()
            rows, meta = limit_pool_hot_view._result(value, 'td' if status == 'RESTORED_FROM_TD' else 'redis',
                                                      None, cutoff, False, None)
            return StoreResult(status=status, rows=rows, metadata=meta, version=value['version'])
        except cold_archive.CorruptArchive as exc:
            return StoreResult(status='CORRUPT', reason=str(exc) or type(exc).__name__)
        except Exception:
            return StoreResult(status='UNKNOWN', reason='read, validation or asof failed')

    def presence(self, dataset, partition, *, asof=None, requirements=None):
        self._dataset(dataset)
        if dataset in ('auction_seal_facts', 'plate_stock_index'):
            raise NotImplementedError('dataset presence is not implemented')
        if asof is not None or requirements is not None:
            raise NotImplementedError('presence asof and requirements are not implemented')
        if dataset == 'hot_rank':
            result = self._hot_rank_get(partition)
            return StoreResult(status=result.status, reason=result.reason)
        status = resolve_presence(dataset, partition, redis_client=_ReadOnlyRedis(self.redis), cold_store=self.cold)
        return StoreResult(status=status, reason=getattr(status, 'reason', None) or ('resolver did not expose cause' if status == 'UNKNOWN' else None))

    def _hot_rank_get(self, partition, asof=None):
        from ..data_pipeline import hot_rank_view
        store = None
        try:
            raw = self.redis.get(f'core:view:hot_rank:{partition}') if self.redis is not None else None
            status = 'PRESENT_REDIS'
            if raw is None:
                if self.cold is None:
                    if not cold_archive._enabled():
                        return StoreResult(status='UNKNOWN', reason='cold_archive_disabled')
                    store = cold_archive.TdStore()
                else:
                    store = self.cold
                version = store.latest_version('hot_rank', partition)
                if version is None:
                    return StoreResult(status='MISSING')
                raw = cold_archive.read_envelope('hot_rank', partition, version, store=store)
                status = 'RESTORED_FROM_TD'
            value = hot_rank_view.validate(raw, partition)
            if status == 'RESTORED_FROM_TD' and value['version'] != version:
                raise ValueError('cold_archive_version_mismatch')
            if asof is not None and datetime.fromisoformat(hot_rank_view._instant(value['available_at'])) > datetime.fromisoformat(hot_rank_view._instant(asof)):
                return StoreResult(status='MISSING', reason='late_visible')
            if status == 'RESTORED_FROM_TD':
                try:
                    if not self.redis.set(f'core:view:hot_rank:{partition}', raw,
                                          exat=hot_rank_view.hot_view_expire_at(partition), nx=True):
                        _LOG.warning('hot_rank_restore_not_written trade_date=%s', partition)
                except Exception:
                    _LOG.warning('hot_rank_restore_failed trade_date=%s', partition, exc_info=True)
            return StoreResult(status=status, rows=value['rows'], metadata=value, version=value['version'])
        except (ValueError, KeyError, TypeError, cold_archive.CorruptArchive) as exc:
            return StoreResult(status='CORRUPT', reason=str(exc))
        except Exception as exc:
            if cold_archive.is_missing_table_error(exc):
                return StoreResult(status='MISSING')
            return StoreResult(status='UNKNOWN', reason='read_failed')
        finally:
            if store is not None and self.cold is None:
                store.close()

    def get_stock_plates(self, partition, symbols):
        from ..data_pipeline.plate_stock_index import read_stocks
        return read_stocks(self, partition, symbols)

    def list_versions(self, dataset, partition_range, *, asof=None):
        raise NotImplementedError('version enumeration needs dataset-specific support')

    def advance(self, dataset, partition, version, *, expected_previous=None):
        raise NotImplementedError('daily_kline latest requires conditional advance support')
