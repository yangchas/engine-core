"""Best-effort, bounded cold copy of a Redis view; Redis remains authoritative."""
from __future__ import annotations

import atexit
import hashlib
import logging
import os
import queue
import re
import sys
import threading
import time
from datetime import date, datetime, timezone
from typing import Any, Mapping

_LOG = logging.getLogger(__name__)
_CHUNK = 6000
# TD rejects the observed 2.2 MB statement; stay below half its 1 MB SQL limit.
MAX_INSERT_SQL_BYTES = 500_000
class CorruptArchive(ValueError):
    """An existing archive failed its integrity checks."""


_DATASET = re.compile(r'^[a-z][a-z0-9_]{0,40}$')


def is_missing_table_error(exc: BaseException) -> bool:
    """Return true only for TD's explicit missing-table errors."""
    text = str(exc).lower()
    return ('0x2603' in text or 'table does not exist' in text or
            'fail to get table info' in text)


def _table(dataset: str, day: str, version: str) -> str:
    if not _DATASET.fullmatch(dataset):
        raise ValueError('invalid_archive_dataset')
    date.fromisoformat(day)
    digest = hashlib.sha256((day + '\0' + version).encode()).hexdigest()[:32]
    return f'cold_{dataset}_{digest}'


def _timestamp(day: str, number: int) -> int:
    # Each identity has its own table, so identical offsets cannot collide.
    base = datetime.combine(date.fromisoformat(day), datetime.min.time(), timezone.utc)
    return int(base.timestamp()) * 1_000_000 + number


def write_envelope(store: Any, dataset: str, trade_date: str, version: str,
                   envelope_bytes: bytes, meta: Mapping[str, Any], *, chunk_size: int = _CHUNK) -> None:
    _table(dataset, trade_date, version)
    if not isinstance(envelope_bytes, bytes) or not 1 <= chunk_size <= _CHUNK:
        raise ValueError('invalid_archive_payload')
    digest = hashlib.sha256(envelope_bytes).hexdigest()
    chunks = [envelope_bytes[i:i + chunk_size] for i in range(0, len(envelope_bytes), chunk_size)] or [b'']
    rows = [dict(ts=_timestamp(trade_date, i), chunk_no=i, chunk_count=len(chunks),
                 sha256=hashlib.sha256(chunk).hexdigest(), payload=chunk)
            for i, chunk in enumerate(chunks)]
    marker = dict(ts=_timestamp(trade_date, len(chunks)), chunk_no=-1, chunk_count=len(chunks),
                  sha256=digest, payload=b'')
    if hasattr(store, 'ensure'):
        store.ensure(dataset, trade_date, version)
    existing = store.read(dataset, trade_date, version)
    expected = {row['chunk_no']: row for row in [*rows, marker]}
    for old in existing:
        candidate = expected.get(old['chunk_no'])
        if candidate is None or any(old[k] != candidate[k] for k in ('chunk_count', 'sha256', 'payload')):
            raise ValueError('cold_archive_identity_conflict')
    store.write(dataset, trade_date, version, rows, meta=meta)
    try:
        store.write(dataset, trade_date, version, [marker], meta=meta)
    except Exception as exc:
        exc.successful_chunks = len(rows)
        raise


def read_envelope(dataset: str, trade_date: str, version: str, *, store: Any = None) -> bytes:
    if store is None:
        if not _enabled():
            raise RuntimeError('cold_archive_disabled')
        store = TdStore()
    rows = store.read(dataset, trade_date, version)
    try:
        markers = [r for r in rows if r['chunk_no'] == -1]
        if len(markers) != 1:
            raise ValueError('cold_archive_incomplete')
        marker = markers[0]
        chunks = [r for r in rows if r['chunk_no'] >= 0]
        chunks.sort(key=lambda r: r['chunk_no'])
        if len(chunks) != marker['chunk_count'] or [r['chunk_no'] for r in chunks] != list(range(len(chunks))):
            raise ValueError('cold_archive_incomplete')
        if any(r['chunk_count'] != len(chunks) or hashlib.sha256(r['payload']).hexdigest() != r['sha256'] for r in chunks):
            raise ValueError('cold_archive_chunk_hash')
        raw = b''.join(r['payload'] for r in chunks)
        if hashlib.sha256(raw).hexdigest() != marker['sha256']:
            raise ValueError('cold_archive_hash')
        return raw
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise CorruptArchive(str(exc) or type(exc).__name__) from exc


def _quote(value: Any) -> str:
    if value is None:
        return 'NULL'
    return "'" + str(value).replace('\\', '\\\\').replace("'", "\\'") + "'"


class TDColdClient:
    """TD SQL adapter; one table per identity isolates timestamp keys."""
    def __init__(self, database=None, *, connect=None):
        self.database = database or os.environ.get('CORE_TD_DATABASE', 'core_data')
        if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]*', self.database):
            raise ValueError('invalid_archive_database')
        from .td_sink import connect_td
        self.conn = connect() if connect else connect_td(self.database)

    def _execute(self, sql: str, fetch: bool = False):
        cursor = self.conn.cursor()
        try:
            cursor.execute(sql)
            return cursor.fetchall() if fetch else None
        finally:
            cursor.close()

    def ensure(self, dataset, day, version):
        table = f'{self.database}.{_table(dataset, day, version)}'
        stable = f'{self.database}.cold_{dataset}_archive'
        self._execute(f'CREATE STABLE IF NOT EXISTS {stable} (ts TIMESTAMP, trade_date VARCHAR(10), '
                      'version VARCHAR(64), event_id VARCHAR(128), cutoff VARCHAR(32), available_at VARCHAR(40), '
                      'provider VARCHAR(64), status VARCHAR(32), chunk_no INT, chunk_count INT, '
                      'sha256 VARCHAR(64), payload VARBINARY(8192)) TAGS (identity VARCHAR(32))')
        identity = _table(dataset, day, version).rsplit('_', 1)[-1]
        self._execute(f"CREATE TABLE IF NOT EXISTS {table} USING {stable} TAGS('{identity}')")

    def write(self, dataset, day, version, rows, *, meta):
        table = f'{self.database}.{_table(dataset, day, version)}'
        prefix = f'INSERT INTO {table} VALUES '
        values = []
        size = len(prefix.encode('utf-8'))
        written = 0
        for row in rows:
            fields = [str(row['ts']), _quote(day), _quote(version), _quote(meta.get('event_id')),
                      _quote(meta.get('cutoff')), _quote(meta.get('available_at')), _quote(meta.get('provider') or meta.get('source')),
                      _quote(meta.get('status')), str(row['chunk_no']), str(row['chunk_count']), _quote(row['sha256']),
                      "'\\x" + row['payload'].hex() + "'"]
            value = '(' + ','.join(fields) + ')'
            value_size = len(value.encode('utf-8'))
            if len(prefix.encode('utf-8')) + value_size > MAX_INSERT_SQL_BYTES:
                exc = ValueError('cold_archive_chunk_exceeds_sql_limit')
                exc.successful_chunks = written
                raise exc
            if values and size + 1 + value_size > MAX_INSERT_SQL_BYTES:
                try:
                    self._execute(prefix + ' '.join(values))
                except Exception as exc:
                    exc.successful_chunks = written
                    raise
                written += len(values)
                values = []
                size = len(prefix.encode('utf-8'))
            size += value_size + bool(values)
            values.append(value)
        if values:
            try:
                self._execute(prefix + ' '.join(values))
            except Exception as exc:
                exc.successful_chunks = written
                raise
        return written + len(values)

    def latest_version(self, dataset, day):
        _table(dataset, day, '')  # validate the interpolated identifiers and date
        stable = f'{self.database}.cold_{dataset}_archive'
        rows = self._execute(f"SELECT version FROM {stable} WHERE trade_date={_quote(day)} "
                             "AND chunk_no=-1 ORDER BY available_at DESC, ts DESC LIMIT 1", fetch=True)
        return str(rows[0][0]) if rows else None

    def read(self, dataset, day, version):
        table = f'{self.database}.{_table(dataset, day, version)}'
        rows = self._execute(f'SELECT chunk_no,chunk_count,sha256,payload FROM {table}', fetch=True)
        return [dict(chunk_no=r[0], chunk_count=r[1], sha256=r[2], payload=bytes(r[3] or b'')) for r in rows]

    def close(self):
        self.conn.close()


TdStore = TDColdClient


class ArchiveReceipt:
    def __init__(self):
        self.success = None
        self.successful_chunks = 0
        self.error_category = None
        self.td_error_code = None
        self.done = threading.Event()

    def __bool__(self):
        return self.success is not False


class Archiver:
    def __init__(self, factory, *, max_queue=128, timeout=5.0, start=True):
        self.factory, self.timeout = factory, timeout
        self.queue = queue.Queue(maxsize=max_queue)
        self.gaps = 0
        if start:
            threading.Thread(target=self._run, daemon=True, name='cold-archive').start()

    def _gap(self, reason, item):
        self.gaps += 1
        _LOG.warning('cold_archive_gap reason=%s dataset=%s trade_date=%s version=%s', reason, *item[:3])

    def submit(self, dataset, trade_date, version, envelope_bytes, meta):
        item = (dataset, trade_date, version, envelope_bytes, dict(meta))
        receipt = ArchiveReceipt()
        try:
            self.queue.put_nowait((item, receipt))
            return receipt
        except queue.Full:
            self._gap('queue_full', item)
            return False

    def flush(self, timeout=5.0):
        deadline = time.monotonic() + max(0, timeout)
        with self.queue.all_tasks_done:
            while self.queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _LOG.warning('cold_archive_flush_timeout')
                    return False
                self.queue.all_tasks_done.wait(remaining)
        return self.gaps == 0

    def _run(self):
        while True:
            item, receipt = self.queue.get()
            errors = []
            def operation():
                store = None
                try:
                    store = self.factory()
                    write_envelope(store, *item)
                except Exception as exc:
                    errors.append(exc)
                finally:
                    if store is not None:
                        try: store.close()
                        except Exception: pass
            task = threading.Thread(target=operation, daemon=True, name='cold-archive-io')
            task.start()
            task.join(self.timeout)
            if task.is_alive():
                self._gap('timeout', item)
                receipt.success = False
                receipt.error_category = 'timeout'
                receipt.done.set()
                # Do not accumulate abandoned I/O threads. Drain later submissions as gaps.
                self.queue.task_done()
                while True:
                    queued_item, queued_receipt = self.queue.get()
                    self._gap('worker_unavailable', queued_item)
                    queued_receipt.success = False
                    queued_receipt.error_category = 'worker_unavailable'
                    queued_receipt.done.set()
                    self.queue.task_done()
            if errors:
                exc = errors[0]
                receipt.success = False
                receipt.successful_chunks = getattr(exc, 'successful_chunks', 0)
                receipt.error_category = type(exc).__name__
                match = re.search(r'0x[0-9a-fA-F]+', str(exc))
                receipt.td_error_code = match.group(0) if match else None
                self._gap(receipt.error_category, item)
            else:
                receipt.success = True
                receipt.successful_chunks = max(1, (len(item[3]) + _CHUNK - 1) // _CHUNK)
            receipt.done.set()
            self.queue.task_done()


def receipt_warning(receipt, flushed):
    if isinstance(receipt, Exception):
        return {'category': type(receipt).__name__, 'td_error_code': None, 'successful_chunks': 0}
    if (receipt is False or
        (hasattr(receipt, 'success') and receipt.success is not True) or
        (not hasattr(receipt, 'success') and not flushed)):
        return {'category': getattr(receipt, 'error_category', None) or 'unavailable_or_timeout',
                'td_error_code': getattr(receipt, 'td_error_code', None),
                'successful_chunks': getattr(receipt, 'successful_chunks', 0)}
    return None


def _enabled():
    return bool(os.environ.get('TD_CONFIG')) and not os.environ.get('CORE_REPLAY_DIR') and 'pytest' not in sys.modules


_worker = None
_lock = threading.Lock()


def archive(dataset, trade_date, version, envelope_bytes, meta):
    global _worker
    if not _enabled():
        return False
    try:
        with _lock:
            if _worker is None:
                _worker = Archiver(TdStore)
        return _worker.submit(dataset, trade_date, version, envelope_bytes, meta)
    except Exception as exc:
        _LOG.warning('cold_archive_gap reason=%s dataset=%s trade_date=%s version=%s',
                     type(exc).__name__, dataset, trade_date, version)
        return False


def flush(timeout=5.0):
    worker = _worker
    if worker is None:
        return True
    try:
        return worker.flush(timeout)
    except Exception:
        _LOG.warning('cold_archive_flush_failed', exc_info=True)
        return False


# Thread shutdown begins before ordinary atexit handlers; flush while the
# worker can still start its bounded I/O thread, with atexit as a fallback.
_exit_flushed = False


def _flush_at_exit():
    global _exit_flushed
    if not _exit_flushed:
        _exit_flushed = True
        flush()


threading._register_atexit(_flush_at_exit)
atexit.register(_flush_at_exit)
