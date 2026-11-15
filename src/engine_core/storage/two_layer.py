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
from ..guardian.presence import resolve_presence


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
        if dataset != 'limit_up_pool':
            raise NotImplementedError('only limit_up_pool is supported')

    def publish(self, dataset, partition, rows, metadata):
        self._dataset(dataset)
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

    def get(self, dataset, partition, *, asof=None, version=None):
        self._dataset(dataset)
        if version is not None:
            raise NotImplementedError('exact version selection is not implemented')
        status = resolve_presence(dataset, partition, redis_client=self.redis, cold_store=self.cold)
        if status not in ('PRESENT_REDIS', 'RESTORED_FROM_TD'):
            return StoreResult(status=status, reason='resolver did not expose cause' if status == 'UNKNOWN' else None)
        try:
            raw = self.redis.get(limit_pool_hot_view._key(partition, 'core:view:limit_up_pool'))
            value = limit_pool_hot_view._validate(json.loads(raw), partition)
            cutoff = asof or datetime.now(timezone.utc).isoformat()
            rows, meta = limit_pool_hot_view._result(value, 'td' if status == 'RESTORED_FROM_TD' else 'redis',
                                                      None, cutoff, False, None)
            return StoreResult(status=status, rows=rows, metadata=meta, version=value['version'])
        except Exception:
            return StoreResult(status='UNKNOWN', reason='read, validation or asof failed')

    def presence(self, dataset, partition, *, asof=None, requirements=None):
        self._dataset(dataset)
        if asof is not None or requirements is not None:
            raise NotImplementedError('presence asof and requirements are not implemented')
        status = resolve_presence(dataset, partition, redis_client=_ReadOnlyRedis(self.redis), cold_store=self.cold)
        return StoreResult(status=status, reason='resolver did not expose cause' if status == 'UNKNOWN' else None)

    def list_versions(self, dataset, partition_range, *, asof=None):
        raise NotImplementedError('version enumeration needs dataset-specific support')

    def advance(self, dataset, partition, version, *, expected_previous=None):
        raise NotImplementedError('daily_kline latest requires conditional advance support')
