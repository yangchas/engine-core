import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from engine_core.data_pipeline import cold_archive, limit_pool_hot_view as hot
from engine_core.guardian import intraday_checkpoint_run as checkpoint, limit_pool_d1
from test_limit_pool_hot_view import RedisSpy
from test_intraday_checkpoint import FakeRedis, req

ROOT = Path(__file__).parent / 'fixtures/real'


def load_fixture(folder, filename, entry=None):
    path = ROOT / folder / filename
    raw = path.read_bytes()
    provenance = json.loads((ROOT / folder / 'PROVENANCE.json').read_text())
    assert hashlib.sha256(raw).hexdigest() == (provenance[entry][filename] if entry else provenance[filename])['sha256']
    return json.loads(raw)


class Store:
    def __init__(self): self.rows = {}
    def read(self, *args): return list(self.rows.values())
    def write(self, dataset, day, version, rows, *, meta):
        self.rows.update((r['chunk_no'], dict(r)) for r in rows)


def test_limit_pool_archive_on_publish_and_ttl_repair(tmp_path, monkeypatch):
    value = load_fixture('c1147', 'limit_up_pool_1112.json')
    day = value['trade_date']; key = 'core:view:limit_up_pool:' + day
    now = datetime.fromisoformat(value['available_at'].replace('Z', '+00:00'))
    redis = RedisSpy(); redis.writes[key] = json.dumps(value, ensure_ascii=False)
    redis.ttl = lambda key: -1
    monkeypatch.setattr(hot, 'deadline_for', lambda *args: int(now.timestamp()) + 10000)
    calls = []; store = Store()
    def archive(*args):
        calls.append(args); cold_archive.write_envelope(store, *args)
    monkeypatch.setattr(cold_archive, 'archive', archive)
    monkeypatch.setattr(cold_archive, 'flush', lambda *args, **kw: True)
    deps = limit_pool_d1.PoolDeps(redis_client=redis, now_utc=lambda: now)
    request = {'data_date': day, 'args': {'source': 'td'}}
    result = limit_pool_d1.run_pool_tier(request, tmp_path, deps=deps)
    assert result[0] == 20 and len(calls) == 1
    dataset, date, version, raw, meta = calls[0]
    assert (dataset, date) == ('limit_up_pool', day) and version.startswith(value['version'] + ':')
    assert raw == redis.writes[key].encode()
    assert cold_archive.read_envelope(dataset, date, version, store=store) == raw
    monkeypatch.setattr(cold_archive, 'archive', lambda *args: (_ for _ in ()).throw(RuntimeError('cold')))
    assert limit_pool_d1.run_pool_tier(request, tmp_path, deps=deps)[0] == 20

    # A fresh upstream publication uses the same captured pool rows and metadata.
    from types import SimpleNamespace
    monkeypatch.setattr(cold_archive, 'archive', archive)
    redis.writes.clear(); calls.clear()
    monkeypatch.setattr(hot, '_td_value', lambda *args: (_ for _ in ()).throw(ValueError('missing')))
    class NoStore:
        def close(self): pass
    deps.td_store = NoStore
    monkeypatch.setattr(limit_pool_d1, '_build', lambda *args: (SimpleNamespace(rows=value['rows'], manifest=value), {}))
    code, body = limit_pool_d1.run_pool_tier(request, tmp_path, deps=deps)
    assert code == 0 and len(calls) == 1
    assert calls[0][3] == redis.writes[key].encode()
    monkeypatch.setattr(cold_archive, 'archive', lambda *args: (_ for _ in ()).throw(RuntimeError('cold')))
    redis.writes.clear()
    assert limit_pool_d1.run_pool_tier(request, tmp_path, deps=deps)[0] == 0

    # The optional TD recovery publication also archives its Redis bytes.
    from engine_core.data_pipeline import limit_pool as core
    from engine_core.data_pipeline.storage import canonical_json, sha256
    recovered = load_fixture('c1147', 'limit_up_pool.json')
    day2 = recovered['trade_date']; redis.writes.clear(); calls.clear()
    columns = core.COMMON_COLUMNS + core.VALUE_COLUMNS + core.KAIPAN_COLUMNS
    rows = [{name: row[name] for name, *_ in columns} for row in sorted(recovered['rows'], key=lambda r: r['symbol'])]
    recovered['source_sha256'] = sha256(b'\n'.join(canonical_json(row) for row in rows) + b'\n')
    monkeypatch.setattr(hot, '_td_value', lambda *args: recovered)
    monkeypatch.setattr(limit_pool_d1, '_build', lambda *args: (_ for _ in ()).throw(core.PoolSourceError('TIER_NO_DATA', 'unavailable', exit_code=40)))
    monkeypatch.setattr(cold_archive, 'archive', archive)
    code, body = limit_pool_d1.run_pool_tier({'data_date': day2, 'args': {'source': 'td'}}, tmp_path, deps=deps)
    assert code == 10 and len(calls) == 1
    assert calls[0][3] == redis.writes['core:view:limit_up_pool:' + day2].encode()


def test_checkpoint_archive_exact_bytes_and_failure(tmp_path, monkeypatch):
    from engine_core.data_pipeline import intraday_checkpoint as core
    row = load_fixture('', 'intraday_checkpoint_c1098_first.jsonl', '_c1098')
    monkeypatch.setattr('engine_core.data_pipeline.redis_day_view.expire_at', lambda day: 2000000000)
    def part(request):
        return core.Partition([row], {'source': 'redis_q2_checkpoint', 'degraded': False,
                              'quality': {'coverage': 1.0}}, {'state': 'ready', 'counts': {}})
    monkeypatch.setattr(core, 'build_intraday_checkpoint', part)
    calls = []; store = Store()
    def archive(*args):
        calls.append(args); cold_archive.write_envelope(store, *args)
    monkeypatch.setattr(cold_archive, 'archive', archive)
    monkeypatch.setattr(cold_archive, 'flush', lambda *args, **kw: True)
    redis = FakeRedis({'000001': {}})
    code, result = checkpoint.run_checkpoint(req(), tmp_path, redis=redis)
    assert code == 0 and len(calls) == 1
    dataset, day, version, raw, meta = calls[0]
    assert dataset == 'intraday_checkpoint' and '09:33:00' in version and len(version) <= 64
    assert raw == redis.values[result['detail']['redis_key']][0].encode()
    assert cold_archive.read_envelope(dataset, day, version, store=store) == raw
    monkeypatch.setattr(cold_archive, 'archive', lambda *args: (_ for _ in ()).throw(RuntimeError('cold')))
    redis2 = FakeRedis({'000001': {}})
    code, failed = checkpoint.run_checkpoint(req(), tmp_path, redis=redis2)
    assert code == 0
    assert failed['detail']['cold_archive_warning']['category'] == 'RuntimeError'
    assert redis2.values == redis.values


def test_formal_pool_publication_archives_once(tmp_path, monkeypatch):
    from types import SimpleNamespace
    value = load_fixture('c1147', 'limit_up_pool_1112_formal.json')
    assert value['business_status'] == 'FORMAL' and value['quality']['status'] == 'READY'
    assert value['degraded'] is False and len(value['rows']) == 37
    now = datetime.fromisoformat(value['available_at'].replace('Z', '+00:00'))
    redis = RedisSpy()
    monkeypatch.setattr(hot, 'deadline_for', lambda *args: int(now.timestamp()) + 10000)
    monkeypatch.setattr(hot, '_td_value', lambda *args: (_ for _ in ()).throw(ValueError('missing')))
    monkeypatch.setattr(limit_pool_d1, '_build', lambda *args: (SimpleNamespace(rows=value['rows'], manifest=value), {}))
    calls = []
    monkeypatch.setattr(cold_archive, 'archive', lambda *args: calls.append(args))
    monkeypatch.setattr(cold_archive, 'flush', lambda *args, **kwargs: True)
    class NoStore:
        def close(self): pass
    deps = limit_pool_d1.PoolDeps(redis_client=redis, now_utc=lambda: now, td_store=NoStore)
    code, body = limit_pool_d1.run_pool_tier({'data_date': value['trade_date'], 'args': {'source': 'kaipan'}}, tmp_path, deps=deps)
    assert code == 0, body
    assert len(calls) == 1
    assert calls[0][3] == redis.writes['core:view:limit_up_pool:' + value['trade_date']].encode()


def test_pool_td_rejection_warns_after_hot_publish(tmp_path, monkeypatch):
    from types import SimpleNamespace
    value = load_fixture('c1147', 'limit_up_pool_1112_formal.json')
    now = datetime.fromisoformat(value['available_at'].replace('Z', '+00:00'))
    redis = RedisSpy()
    class RejectStore:
        def read(self, *args): return []
        def write(self, *args, **kwargs): raise RuntimeError('TD rejected 0x2603')
        def close(self): pass
    monkeypatch.setattr(cold_archive, '_worker', cold_archive.Archiver(RejectStore))
    monkeypatch.setattr(cold_archive, '_enabled', lambda: True)
    monkeypatch.setattr(hot, 'deadline_for', lambda *args: int(now.timestamp()) + 10000)
    monkeypatch.setattr(hot, '_td_value', lambda *args: (_ for _ in ()).throw(ValueError('missing')))
    monkeypatch.setattr(limit_pool_d1, '_build', lambda *args: (SimpleNamespace(rows=value['rows'], manifest=value), {}))
    deps = limit_pool_d1.PoolDeps(redis_client=redis, now_utc=lambda: now, td_store=RejectStore)
    code, body = limit_pool_d1.run_pool_tier({'data_date': value['trade_date'], 'args': {'source': 'kaipan'}}, tmp_path, deps=deps)
    assert code == 0 and redis.writes
    assert body['detail']['hot_write']['cold_archive_warning'] == {'category': 'RuntimeError', 'td_error_code': '0x2603', 'successful_chunks': 0}
