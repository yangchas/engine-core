"""C1293: invert captured Kaipan memberships without inventing input rows."""
import json
from collections import Counter
from pathlib import Path


def captured_members():
    import gzip
    path = Path(__file__).parent / 'fixtures/real/core_artifacts/c594/plate_members/2026-10-28/current/rows.jsonl.gz'
    with gzip.open(path, 'rt') as stream:
        return [json.loads(line) for line in stream]


def test_real_memberships_invert_without_losing_overlap():
    from engine_core.data_pipeline.plate_stock_index import build_index
    source = captured_members()
    index = build_index(source)
    expected = Counter((row['symbol'], row['plate_code'], row['plate_class'], row['plate_member_label']) for row in source)
    actual = Counter((symbol, plate['plate_code'], plate['plate_class'], plate['plate_member_label'])
                     for symbol, plates in index.items() for plate in plates)
    assert actual == expected
    assert any(len(plates) > 1 for plates in index.values())
    assert index == build_index(source)


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.hashes = {}
        self.fail = False

    def pipeline(self, **kwargs):
        return Pipeline(self)

    def get(self, key):
        if self.fail: raise ConnectionError('redis unavailable')
        return self.values.get(key)

    def delete(self, key):
        if self.fail: raise ConnectionError('redis unavailable')
        self.values.pop(key, None); self.hashes.pop(key, None)

    def hlen(self, key):
        return len(self.hashes.get(key, {}))


class Pipeline:
    def __init__(self, redis):
        self.redis = redis
        self.commands = []
        self.watching = False

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def watch(self, key): self.watching = True
    def unwatch(self): self.watching = False
    def multi(self): self.watching = False
    def get(self, key):
        if self.watching:
            return self.redis.values.get(key)
        self.commands.append(('get', key, None))
        return self
    def delete(self, *keys):
        self.commands.append(('delete', keys, None)); return self
    def hset(self, key, mapping):
        self.commands.append(('hset', key, mapping)); return self
    def hgetall(self, key):
        self.commands.append(('hgetall', key, None)); return self
    def hmget(self, key, symbols):
        self.commands.append(('hmget', key, symbols)); return self
    def expireat(self, key, deadline): return self
    def set(self, key, value, **kwargs):
        self.commands.append(('set', key, value)); return self
    def execute(self):
        if self.redis.fail:
            raise ConnectionError('redis unavailable')
        out = []
        for op, key, arg in self.commands:
            if op == 'get': out.append(self.redis.values.get(key))
            elif op == 'set': self.redis.values[key] = arg; out.append(True)
            elif op == 'hset': self.redis.hashes[key] = dict(arg); out.append(len(arg))
            elif op == 'hgetall': out.append(self.redis.hashes.get(key, {}).copy())
            elif op == 'hmget': out.append([self.redis.hashes.get(key, {}).get(s) for s in arg])
            elif op == 'delete':
                for k in key:
                    self.redis.values.pop(k, None); self.redis.hashes.pop(k, None)
                out.append(1)
        self.commands.clear()
        return out


class MemoryCold:
    def __init__(self): self.versions = {}
    def read(self, dataset, day, version): return self.versions.get(version, [])
    def write(self, dataset, day, version, rows, *, meta):
        current = {row['chunk_no']: row for row in self.versions.get(version, [])}
        current.update({row['chunk_no']: row for row in rows})
        self.versions[version] = list(current.values())
    def latest_version(self, dataset, day): return next(reversed(self.versions), None)


def test_real_data_storage_failure_receipts_and_missing(monkeypatch):
    from engine_core.data_pipeline import cold_archive, plate_stock_index as index
    from engine_core.storage.two_layer import TwoLayerStore
    members = [row for row in captured_members() if row['plate_code'] == '801050']
    rows = [{'symbol': s, 'plates': p} for s, p in index.build_index(members).items()]
    meta = {'version': 'psi1:20261116010016-dce913bc3963', 'member_version': '20261116010016-dce913bc3963',
            'asof': '2026-10-28T09:26:00+08:00', 'available_at': '2026-10-28T09:26:00+08:00',
            'member_available_at': '2026-10-28T09:20:00+08:00'}
    redis, cold = MemoryRedis(), MemoryCold()
    store = TwoLayerStore(redis, cold)
    monkeypatch.setattr('engine_core.data_pipeline.redis_day_view.expire_at', lambda _: 2000000000)
    def archive(dataset, day, version, payload, metadata):
        receipt = cold_archive.ArchiveReceipt()
        cold_archive.write_envelope(cold, dataset, day, version, payload, metadata)
        receipt.success = True; receipt.done.set()
        return receipt
    monkeypatch.setattr(cold_archive, 'archive', archive)
    result = store.publish('plate_stock_index', '2026-10-28', rows, meta)
    assert result.status == 'published' and result.archive_status == 'ARCHIVED'
    result2 = store.publish('plate_stock_index', '2026-10-28', rows, meta)
    assert result.version == result2.version
    picked = rows[0]['symbol']
    missing = next(f'{i:06d}' for i in range(1000) if f'{i:06d}' not in {r['symbol'] for r in rows})
    fetched = store.get_stock_plates('2026-10-28', [picked, missing])
    assert fetched[picked]['plates'] == rows[0]['plates'] and fetched[picked]['version'] == result.version
    assert fetched[missing]['status'] == 'MISSING' and 'plates' not in fetched[missing]
    redis.hashes['core:view:plate_stock_index:2026-10-28'].pop(picked)
    restored = store.get_stock_plates('2026-10-28', [picked])
    assert restored[picked]['status'] == 'RESTORED_FROM_TD'
    assert restored[picked]['version'] == result.version and restored[picked]['plates'] == rows[0]['plates']
    empty_cold = TwoLayerStore(redis, MemoryCold())
    partial = empty_cold.get_stock_plates('2026-10-28', [picked, missing])
    assert partial[picked]['status'] == 'PRESENT_REDIS'
    assert partial[missing]['status'] == 'UNKNOWN'
    redis.fail = True
    assert store.publish('plate_stock_index', '2026-10-28', rows, meta).status == 'FAILED'
    redis.fail = False
    class RejectedCold(MemoryCold):
        def write(self, dataset, day, version, rows, *, meta):
            raise RuntimeError('TD batch rejected')
        def close(self): pass
    archiver = cold_archive.Archiver(RejectedCold)
    monkeypatch.setattr(cold_archive, 'archive', archiver.submit)
    failure = store.publish('plate_stock_index', '2026-10-28', rows, meta)
    assert failure.status == 'published' and failure.archive_status == 'FAILED'
    assert store.get_stock_plates('2026-10-28', [picked])[picked]['status'] == 'PRESENT_REDIS'


def test_captured_markers_select_distinct_index_versions(monkeypatch):
    from engine_core.data_pipeline import plate_stock_index as index
    from engine_core.data_pipeline.reference_snapshot_td import ReferenceSnapshotStore
    markers = json.loads((Path(__file__).parent / 'fixtures/real/c1287/markers.json').read_text())
    class Selected(Exception): pass
    ref = ReferenceSnapshotStore(connect=lambda: None)
    monkeypatch.setattr(ref, '_run', lambda *args, **kwargs: markers)
    def selected(dataset, version): raise Selected(version)
    monkeypatch.setattr(ref, '_rows_for_version', selected)
    from engine_core.data_pipeline import cold_archive
    from engine_core.storage.two_layer import TwoLayerStore
    redis = MemoryRedis()
    store = TwoLayerStore(redis, MemoryCold())
    monkeypatch.setattr('engine_core.data_pipeline.redis_day_view.expire_at', lambda _: 2000000000)
    def accepted(*args):
        receipt = cold_archive.ArchiveReceipt(); receipt.success = True; receipt.done.set(); return receipt
    monkeypatch.setattr(cold_archive, 'archive', accepted)
    versions = []
    saved = []
    for asof in ('2026-11-16T09:26:00+08:00', '2026-11-16T15:30:00+08:00'):
        try:
            ref.read_as_of('plate_members', asof)
        except Selected as exc:
            member_version = str(exc)
        else:
            raise AssertionError('expected a captured ACTIVE marker')
        mark = next(m for m in markers if m[0] == member_version)
        source = [row for row in captured_members() if row['plate_code'] == '801050']
        rows = [{'symbol': s, 'plates': p} for s, p in index.build_index(source).items()]
        meta = {'version': member_version + ':' + asof[0:10].replace('-', '') + asof[11:19].replace(':', ''), 'member_version': member_version,
                'asof': asof, 'available_at': asof, 'member_available_at': mark[4]}
        result = store.publish('plate_stock_index', '2026-11-16', rows, meta)
        assert result.status == 'published'
        versions.append(result.version)
        saved.append((rows, meta))
    assert versions[0] != versions[1]
    assert '20261116010016-dce913bc3963' in versions[0]
    assert '20261116070002-a97de1bbedc6' in versions[1]
    assert store.get_stock_plates('2026-11-16', [rows[0]['symbol']])[rows[0]['symbol']]['version'] == versions[1]

    class Reference:
        def read_as_of(self, dataset, asof):
            return {'rows': source, 'version': member_version, 'available_at': mark[4]}
    class Capture:
        def __init__(self): self.versions = []
        def publish(self, dataset, day, rows, metadata):
            self.versions.append(metadata['version'])
    capture = Capture()
    for asof in ('2026-11-16T15:30:00.000001+08:00', '2026-11-16T15:30:00.000002+08:00'):
        index.run('2026-11-16', asof, reference_store=Reference(), store=capture)
    assert capture.versions[0] != capture.versions[1]
