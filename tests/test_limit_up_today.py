"""C900 regression using the captured 2026-10-28 Q2 snapshot."""
import json
from datetime import datetime
from pathlib import Path

from engine_core.guardian import limit_up_today as runner


def test_suspended_stale_only_does_not_degrade(tmp_path, monkeypatch):
    fixture = Path(__file__).parent / 'fixtures/real/q2_limit_up_pool_today_2026-10-28_0702.json'
    raw = json.loads(fixture.read_text())
    symbols = ['000016', '000554']
    snapshot = {'active_symbols': symbols, 'hashes': {s: raw['hashes'][s] for s in symbols}}
    monkeypatch.setattr(runner, '_same_date_universe_names', lambda *_: {'000016': '深康佳A', '000554': '泰山石油'})
    now = datetime.fromisoformat('2026-10-28T15:10:00+08:00')
    deps = runner.TodayDeps(redis_snapshot=lambda _: snapshot, clock=lambda: now,
                            publish_hot=lambda *_: {'status': 'offline_replay'})
    code, reply = runner.run({'data_date': '2026-10-28', 'as_of': now.isoformat()}, tmp_path, deps=deps)
    assert code == 0, reply
    manifest = json.loads(Path(reply["artifacts"][0]).read_text())
    assert manifest["quality_details"]["limit_pool"]["suspended_stale"] == 1


def test_unconfirmed_cross_day_stays_degraded(tmp_path, monkeypatch):
    raw = json.loads((Path(__file__).parent / 'fixtures/real/q2_limit_up_pool_today_2026-10-28_0702.json').read_text())
    symbols = ['301139', '000554']
    snapshot = {'active_symbols': symbols, 'hashes': {s: raw['hashes'][s] for s in symbols}}
    monkeypatch.setattr(runner, '_same_date_universe_names', lambda *_: {'000554': '泰山石油'})
    now = datetime.fromisoformat('2026-10-28T15:10:00+08:00')
    code, reply = runner.run({'data_date': '2026-10-28', 'as_of': now.isoformat()}, tmp_path,
                             deps=runner.TodayDeps(redis_snapshot=lambda _: snapshot, clock=lambda: now,
                                                   publish_hot=lambda *_: {'status': 'offline_replay'}))
    assert code == 10
    assert reply['detail']['stale_cross_day'] == [{'symbol': '301139', 'trade_date': '2026-10-27'}]
    assert 'stale_quote=1' in reply['detail']['reason']


def test_future_quote_stays_degraded(tmp_path, monkeypatch):
    raw = json.loads((Path(__file__).parent / 'fixtures/real/q2_limit_up_pool_today_2026-10-28_0702.json').read_text())
    symbols = ['000554']
    snapshot = {'active_symbols': symbols, 'hashes': {s: raw['hashes'][s] for s in symbols}}
    monkeypatch.setattr(runner, '_same_date_universe_names', lambda *_: {'000554': '泰山石油'})
    now = datetime.fromisoformat('2026-10-28T15:00:00+08:00')
    # A real post-sample quote plus another real same-day quote available at the cutoff.
    candidates = [s for s, h in raw['hashes'].items() if runner.core._source_time(h.get('ts'))
                  and runner.core._source_time(h.get('ts'))[1].startswith('2026-10-28T15:00:02')]
    assert candidates
    symbols.append(candidates[0]); snapshot['hashes'][candidates[0]] = raw['hashes'][candidates[0]]
    code, reply = runner.run({'data_date': '2026-10-28', 'as_of': now.isoformat()}, tmp_path,
                             deps=runner.TodayDeps(redis_snapshot=lambda _: snapshot, clock=lambda: now,
                                                   publish_hot=lambda *_: {'status': 'offline_replay'}))
    assert code == 10
    assert 'future_quote=1' in reply['detail']['reason']


def test_registered_hot_publisher_verifies_and_rolls_back(tmp_path, monkeypatch):
    import pytest
    from engine_core.data_pipeline.providers.limit_up_today_sources import publish_hot_snapshot, SourceUnavailable
    from engine_core.data_pipeline.registry import get_dataset

    class Redis:
        def __init__(self): self.values, self.ttls, self.bad = {}, {}, False
        def get(self, k): return self.values.get(k)
        def ttl(self, k): return -1 if self.bad and k.endswith(':latest') else self.ttls.get(k, -2)
        def set(self, k, v, ex=None): self.values[k] = v; self.ttls[k] = ex or -1
        def delete(self, k): self.values.pop(k, None); self.ttls.pop(k, None)

    raw = json.loads((Path(__file__).parent / 'fixtures/real/q2_limit_up_pool_today_2026-10-28_0702.json').read_text())
    now = datetime.fromisoformat('2026-10-28T15:10:00+08:00')
    built = runner._build_pool('2026-10-28', now, {'active_symbols': ['000554'], 'hashes': raw['hashes']}, {'000554': '泰山石油'})
    manifest = runner._manifest('2026-10-28', now, built['rows'], provider='redis_q2', provider_chain=['redis_q2'], status='FORMAL', attempts=[], quality={}, details={})
    manifest['version'] = 'test'
    client = Redis()
    result = publish_hot_snapshot(manifest, built['rows'], client=runner._VerifiedRedis(client))
    assert result['ttl_seconds'] == get_dataset('limit_up_pool_today').redis_ttl_seconds
    assert set(result['keys']) == {'core:cache:data:limit_up_pool_today:2026-10-28:test', 'core:cache:data:limit_up_pool_today:2026-10-28', 'core:cache:data:limit_up_pool_today:latest'}
    assert all(client.ttl(k) == result['ttl_seconds'] for k in result['keys'])
    previous = dict(client.values)
    manifest['version'] = 'test2'
    client.bad = True
    with pytest.raises(SourceUnavailable):
        publish_hot_snapshot(manifest, built['rows'], client=runner._VerifiedRedis(client))
    assert client.values == previous


def test_handler_dry_run_never_connects_redis(monkeypatch, tmp_path):
    import redis
    from examples import guardian_handler
    from engine_core.guardian.contract import check_reply
    monkeypatch.setenv('GUARDIAN_DRY_RUN', '1')
    monkeypatch.setenv('CORE_DATA_ROOT', str(tmp_path))
    def forbidden(*args, **kwargs):
        raise AssertionError('dry run connected to Redis')
    monkeypatch.setattr(redis.Redis, 'from_url', forbidden)
    code, reply = guardian_handler._run({'event': 'limit_up_today_110000', 'trade_date': '2026-10-28'})
    assert code == 0 and not check_reply(code, reply)
    assert reply['detail'] == 'dry_run'
