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
    published = []
    deps = runner.TodayDeps(redis_snapshot=lambda _: snapshot, clock=lambda: now,
                            publish_hot=lambda manifest, rows: published.append(manifest) or {'status': 'offline_replay'})
    code, reply = runner.run({'data_date': '2026-10-28', 'as_of': now.isoformat()}, tmp_path, deps=deps)
    assert code == 0, reply
    manifest = published[0]
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


def test_scan_completion_sets_pit_cutoff(tmp_path, monkeypatch):
    raw = json.loads((Path(__file__).parent / 'fixtures/real/q2_limit_up_pool_today_2026-10-28_0702.json').read_text())
    candidate = next(s for s, h in raw['hashes'].items() if runner.core._source_time(h.get('ts'))
                     and runner.core._source_time(h.get('ts'))[1].startswith('2026-10-28T15:00:02'))
    monkeypatch.setattr(runner, '_same_date_universe_names', lambda *_: {'000554': '泰山石油', candidate: '正常'})
    observed = datetime.fromisoformat('2026-10-28T15:00:03+08:00')
    for future in (False, True):
        hashes = {s: dict(raw['hashes'][s]) for s in ('000554', candidate)}
        if future:
            # Fault injection into a captured quote; no fixture bytes are changed.
            hashes[candidate]['ts'] = int(datetime.fromisoformat('2026-10-28T15:00:05+08:00').timestamp() * 1000)
        snapshot = {'active_symbols': ['000554', candidate], 'hashes': hashes}
        published = []
        deps = runner.TodayDeps(redis_snapshot=lambda _: snapshot, clock=lambda: observed,
                                publish_hot=lambda manifest, rows: published.append(manifest) or {'status': 'offline_replay'})
        code, reply = runner.run({'data_date': '2026-10-28', 'as_of': '2026-10-28T15:00:00+08:00'}, tmp_path, deps=deps)
        assert reply['detail']['as_of'] == observed.isoformat()
        assert published[0]['available_at'] == observed.isoformat()
        assert published[0]['quality_details']['limit_pool']['future_quote'] == int(future)
        assert code == (10 if future else 0)
        if future:
            assert 'future_quote=1' in reply['detail']['reason']


def test_long_scan_delay_and_historical_replay_cutoffs(tmp_path, monkeypatch):
    raw = json.loads((Path(__file__).parent / 'fixtures/real/q2_limit_up_pool_today_2026-10-28_0702.json').read_text())
    candidate = next(s for s, h in raw['hashes'].items() if runner.core._source_time(h.get('ts'))
                     and runner.core._source_time(h.get('ts'))[1].startswith('2026-10-28T15:00:02'))
    symbols = ['000554', candidate]
    snapshot = {'active_symbols': symbols, 'hashes': {s: raw['hashes'][s] for s in symbols}}
    monkeypatch.setattr(runner, '_same_date_universe_names', lambda *_: {s: '正常' for s in symbols})
    requested = '2026-10-28T14:58:28+08:00'
    sample_times = []
    build_pool = runner._build_pool
    def capture_sample_time(day, sample_time, snapshot, names):
        sample_times.append(sample_time.isoformat())
        return build_pool(day, sample_time, snapshot, names)
    monkeypatch.setattr(runner, '_build_pool', capture_sample_time)
    for observed, expected, historical in (
        ('2026-10-29T15:00:03+08:00', requested, True),
        ('2026-10-28T15:00:03+08:00', '2026-10-28T15:00:03+08:00', False),
    ):
        published = []
        deps = runner.TodayDeps(redis_snapshot=lambda _: snapshot,
                                clock=lambda: datetime.fromisoformat(observed),
                                publish_hot=lambda manifest, rows: published.append(manifest) or {'status': 'offline_replay'})
        code, reply = runner.run({'data_date': '2026-10-28', 'as_of': requested}, tmp_path, deps=deps)
        assert sample_times[-1] == expected
        if historical:
            assert code == 40 and reply['error_code'] == 'Q2_SNAPSHOT_INVALID'
            assert not published
            continue
        assert reply['detail']['as_of'] == expected
        assert published[0]['as_of'] == expected
        assert published[0]['available_at'] == expected
        assert published[0]['quality_details']['limit_pool']['future_quote'] == 0
        assert code == 0


def test_registered_hot_publisher_verifies_and_rolls_back(tmp_path, monkeypatch):
    import pytest
    from engine_core.data_pipeline.providers.limit_up_today_sources import publish_hot_snapshot, SourceUnavailable
    from engine_core.data_pipeline import redis_day_view

    monkeypatch.setenv('CORE_DATA_ROOT', '/home/exedev/core-data')
    clock = datetime.fromisoformat('2026-10-28T15:10:00+08:00').timestamp()
    monkeypatch.setattr(redis_day_view, '_now_timestamp', lambda: clock)

    class Redis:
        def __init__(self): self.values, self.ttls, self.bad = {}, {}, False
        def get(self, k): return self.values.get(k)
        def ttl(self, k): return -1 if self.bad and k.endswith(':latest') else self.ttls.get(k, -2)
        def set(self, k, v, ex=None, exat=None):
            self.values[k] = v
            self.ttls[k] = ex if ex is not None else int(exat - clock) if exat is not None else -1
        def delete(self, k): self.values.pop(k, None); self.ttls.pop(k, None)

    raw = json.loads((Path(__file__).parent / 'fixtures/real/q2_limit_up_pool_today_2026-10-28_0702.json').read_text())
    now = datetime.fromisoformat('2026-10-28T15:10:00+08:00')
    built = runner._build_pool('2026-10-28', now, {'active_symbols': ['000554'], 'hashes': raw['hashes']}, {'000554': '泰山石油'})
    manifest = runner._manifest('2026-10-28', now, built['rows'], provider='redis_q2', provider_chain=['redis_q2'], status='FORMAL', attempts=[], quality={}, details={})
    manifest['version'] = 'test'
    client = Redis()
    result = publish_hot_snapshot(manifest, built['rows'], client=runner._VerifiedRedis(client))
    assert result['ttl_seconds'] == redis_day_view.hot_view_expire_at('2026-10-28') - int(clock)
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


def test_today_sample_publishes_without_catalog(tmp_path, monkeypatch):
    raw = json.loads((Path(__file__).parent / 'fixtures/real/q2_limit_up_pool_today_2026-10-28_0702.json').read_text())
    snapshot = {'active_symbols': ['000554'], 'hashes': raw['hashes']}
    monkeypatch.setattr(runner, '_same_date_universe_names', lambda *_: {'000554': '泰山石油'})
    now = datetime.fromisoformat('2026-10-28T15:10:00+08:00')
    published = []
    def publish(manifest, rows):
        published.append((manifest, rows))
        return {'status': 'published'}
    code, reply = runner.run({'data_date': '2026-10-28', 'as_of': now.isoformat()}, tmp_path,
                             deps=runner.TodayDeps(redis_snapshot=lambda _: snapshot, clock=lambda: now,
                                                   publish_hot=publish))
    assert code == 0, reply
    assert published and published[0][0]['version']
    assert reply['artifacts'] == []
    assert not (tmp_path / 'catalog.json').exists()
    assert not (tmp_path / 'limit_up_pool_today').exists()


def test_realtime_seal_status_from_c1574_tick():
    record = json.loads(Path('/home/exedev/fw/evidence/C1574/real_ticks.json').read_text())[0]['rows'][0]
    timestamp = int(datetime.fromisoformat(record[0] + '+08:00').timestamp() * 1000)
    base = {'px': str(record[2]), 'pc': str(record[3]), 'ts': str(timestamp), 'br': '641160600'}
    def row(extra):
        return runner.core.build_redis_pool('2026-11-25', '2026-11-25T09:36:00+08:00', [record[1]], {record[1]: {**base, **extra}})['rows'][0]
    sealed = row({'b1a': str(record[6] * record[8] // 10), 'a1a': str(record[7] * record[9] // 10)})
    assert (sealed['seal_amount'], sealed['ask1_amount'], sealed['seal_status']) == (641160600.0, 0.0, 'SEALED')
    opened = row({'b1a': '9300000', 'a1a': '223200'})
    assert (opened['seal_amount'], opened['ask1_amount'], opened['seal_status']) == (0.0, 223200.0, 'OPEN_RISK')
    empty = row({'b1a': '0', 'a1a': '0'})
    assert (empty['seal_amount'], empty['ask1_amount'], empty['seal_status']) == (0.0, 0.0, 'EMPTY_BOOK')
    for extra in ({}, {'b1a': '1'}, {'b1a': '-1', 'a1a': '0'}, {'b1a': '1.5', 'a1a': '0'}, {'b1a': 'bad', 'a1a': '0'}):
        old = row(extra)
        assert (old['seal_amount'], old.get('ask1_amount'), old.get('seal_status')) == (None, None, None)


def test_degrade_requested_falls_back_to_recorded_wencai(tmp_path):
    from unittest.mock import Mock
    from engine_core.data_pipeline.providers.limit_up_today_sources import SourceUnavailable
    rows = json.loads((Path(__file__).parent / 'fixtures/real/wencai_limit_up_pool_today_2026-10-28_0702.json').read_text())
    now = datetime.fromisoformat('2026-10-28T10:30:00+08:00')
    def unavailable(_):
        raise SourceUnavailable('Q2 unavailable')
    fetch = Mock(return_value={'records': rows, 'reported_total': len(rows), 'complete': True, 'truncated': False})
    state = Mock()
    state.claim.return_value = True
    deps = runner.TodayDeps(redis_snapshot=unavailable, wencai_snapshot=fetch, clock=lambda: now,
                            state=state, publish_hot=lambda *_: {'status': 'offline_replay'})
    request = {'data_date': '2026-10-28', 'as_of': now.isoformat(),
               'args': {'tier': 'redis', 'failure_fallback': 'none'}}
    code, reply = runner.run({**request, 'degrade_requested': False}, tmp_path, deps=deps)
    assert code == 75 and not fetch.called
    code, reply = runner.run({**request, 'degrade_requested': True}, tmp_path, deps=deps)
    assert code in (0, 10) and reply['detail']['row_count'] > 0
    assert fetch.call_count == 1
