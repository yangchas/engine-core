"""Prototype for the previous-day limit pool's existing hot and cold stores.

Only limit_up_pool is supported. Version enumeration and conditional advance
(in particular daily_kline latest) need dataset-specific contracts first.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Protocol

from ..data_pipeline import cold_archive, limit_pool_hot_view


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
        cutoff = asof or datetime.now(timezone.utc).isoformat()
        rows, meta = limit_pool_hot_view.read(partition, cutoff, redis_client=self.redis, cold_store=self.cold)
        status = meta['status']
        if rows and meta.get('storage_source') == 'redis' and status == 'READY':
            status = 'PRESENT_REDIS'
        return StoreResult(status=status, rows=rows, metadata=meta,
                           version=meta.get('version'), reason=meta.get('reason'))

    def restore(self, dataset, partition):
        """Explicitly restore a validated missing hot pool, then verify the write."""
        if dataset == 'hot_rank':
            return self._restore_hot_rank(partition)
        if dataset != 'limit_up_pool':
            raise NotImplementedError('restore only supports limit_up_pool')
        result = self.get(dataset, partition)
        rows, meta, status = result.rows, dict(result.metadata), result.status
        cutoff = datetime.now(timezone.utc).isoformat()
        if rows and meta.get('storage_source') in ('cold', 'td') and meta.get('fallback_reason') == 'redis_missing':
            archive = None
            owned = False
            try:
                if meta['storage_source'] == 'cold':
                    archive = self.cold
                    owned = archive is None
                    if owned:
                        if not cold_archive._enabled():
                            raise RuntimeError('cold_archive_disabled')
                        archive = cold_archive.TdStore()
                    version = meta['archive_version']
                    raw = cold_archive.read_envelope(dataset, partition, version, store=archive)
                    value = limit_pool_hot_view._validate(json.loads(raw), partition)
                    hashed = value['version'] + ':' + hashlib.sha256(raw).hexdigest()[:16]
                    if version not in (value['version'], hashed):
                        raise ValueError('restore_candidate_changed')
                else:
                    archive = limit_pool_hot_view._td_store()
                    owned = True
                    value = limit_pool_hot_view._td_value(partition, archive)
                    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()
                candidate_rows, candidate_meta = limit_pool_hot_view._result(value, meta['storage_source'], None, cutoff, False, None)
                if (value['version'] != meta['version'] or candidate_rows != rows or
                        candidate_meta['quality'] != meta['quality'] or
                        candidate_meta['available_at'] != meta['available_at'] or
                        candidate_meta['business_status'] != meta['business_status'] or
                        candidate_meta['degraded'] != meta['degraded'] or
                        candidate_meta['source'] != meta['source'] or
                        candidate_meta['provider'] != meta['provider']):
                    raise ValueError('restore_candidate_changed')
                from ..data_pipeline.redis_day_view import hot_view_expire_at
                key = limit_pool_hot_view._key(partition, 'core:view:limit_up_pool')
                written = self.redis.set(key, raw, exat=hot_view_expire_at(partition), nx=True)
                observed = self.redis.get(key)
                observed_value = limit_pool_hot_view._validate(json.loads(observed), partition)
                observed_rows, observed_meta = limit_pool_hot_view._result(observed_value, 'redis', None, cutoff, False, None)
                if written and observed == raw:
                    meta['restore_status'] = 'RESTORED_FROM_TD'
                    if (status == 'DEGRADED' and meta['quality'].get('status') == 'READY' and
                            not meta['quality'].get('partial') and meta['business_status'] == 'FORMAL' and
                            not meta['degraded']):
                        status = 'RESTORED_FROM_TD'
                elif not written:
                    rows, meta = observed_rows, observed_meta
                    meta['restore_status'] = 'PRESENT_REDIS'
                    status = 'PRESENT_REDIS' if meta['status'] == 'READY' else meta['status']
                else:
                    rows, meta = observed_rows, observed_meta
                    meta['restore_status'] = 'PRESENT_REDIS'
                    status = 'PRESENT_REDIS' if meta['status'] == 'READY' else meta['status']
            except Exception as exc:
                meta['restore_status'] = 'UNKNOWN'
                meta['reason'] = 'restore_' + type(exc).__name__
                meta['selection_uncertain'] = True
            finally:
                if owned and archive is not None:
                    try: archive.close()
                    except Exception: pass
        return StoreResult(status=status, rows=rows, metadata=meta,
                           version=meta.get('version'), reason=meta.get('reason'))

    def _restore_hot_rank(self, partition):
        from ..data_pipeline import hot_rank_view
        result = self._hot_rank_get(partition)
        if result.status != 'DEGRADED':
            return result
        meta = dict(result.metadata)
        archive = self.cold
        owned = False
        try:
            if archive is None:
                archive = cold_archive.TdStore()
                owned = True
            version = archive.latest_version('hot_rank', partition)
            raw = cold_archive.read_envelope('hot_rank', partition, version, store=archive)
            value = hot_rank_view.validate(raw, partition)
            if version != result.version or value != result.metadata:
                raise ValueError('restore_candidate_changed')
            key = f'core:view:hot_rank:{partition}'
            written = self.redis.set(key, raw, exat=hot_rank_view.hot_view_expire_at(partition), nx=True)
            observed = self.redis.get(key)
            observed_value = hot_rank_view.validate(observed, partition)
            if written and (observed.encode() if isinstance(observed, str) else observed) == (raw.encode() if isinstance(raw, str) else raw):
                return StoreResult(status='RESTORED_FROM_TD', rows=value['rows'], metadata=value, version=version)
            if not written:
                return StoreResult(status='PRESENT_REDIS', rows=observed_value['rows'],
                                   metadata=observed_value, version=observed_value['version'])
            raise ValueError('restore_readback_mismatch')
        except Exception as exc:
            meta['restore_status'] = 'UNKNOWN'
            meta['reason'] = 'restore_' + type(exc).__name__
            meta['selection_uncertain'] = True
            return StoreResult(status=result.status, rows=result.rows, metadata=meta,
                               version=result.version, reason=meta['reason'])
        finally:
            if owned and archive is not None:
                try: archive.close()
                except Exception: pass

    def presence(self, dataset, partition, *, asof=None, requirements=None):
        self._dataset(dataset)
        if dataset in ('auction_seal_facts', 'plate_stock_index'):
            raise NotImplementedError('dataset presence is not implemented')
        if requirements is not None or (asof is not None and dataset != 'limit_up_pool'):
            raise NotImplementedError('presence asof and requirements are not implemented')
        if dataset == 'hot_rank':
            result = self._hot_rank_get(partition)
            return StoreResult(status=result.status, reason=result.reason)
        result = self.get(dataset, partition, asof=asof)
        return StoreResult(status=result.status, metadata=result.metadata,
                           version=result.version, reason=result.reason)

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
                status = 'DEGRADED'
            value = hot_rank_view.validate(raw, partition)
            if status == 'DEGRADED' and value['version'] != version:
                raise ValueError('cold_archive_version_mismatch')
            if asof is not None and datetime.fromisoformat(hot_rank_view._instant(value['available_at'])) > datetime.fromisoformat(hot_rank_view._instant(asof)):
                return StoreResult(status='MISSING', reason='late_visible')
            return StoreResult(status=status, rows=value['rows'], metadata=value, version=value['version'])
        except (ValueError, KeyError, TypeError, cold_archive.CorruptArchive):
            return StoreResult(status='CORRUPT', reason='invalid_hot_rank_or_archive')
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
