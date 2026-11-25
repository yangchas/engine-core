"""Recorded-pool contract; run with python -B and an empty data-source environment."""
import os
import sys

sys.dont_write_bytecode = True
if __name__ == '__main__' and any(os.environ.get(k) for k in ('REDIS_URL', 'CORE_REDIS_URL', 'TD_CONFIG', 'TD_HOST')):
    raise RuntimeError('data_source_environment_not_empty')

def audit(event, args):
    if event == 'open':
        mode, flags = args[1:3]
        if (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
            raise RuntimeError('file_write_blocked')
    if event.startswith(('socket.', 'subprocess.')) or event in ('os.system', 'os.remove', 'os.rename', 'os.mkdir', 'os.rmdir'):
        raise RuntimeError('side_effect_blocked')
if __name__ == '__main__':
    sys.addaudithook(audit)

import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from engine_core.data_pipeline import cold_archive, limit_pool_hot_view as pool
from engine_core.guardian.presence import resolve_presence
from engine_core.storage.two_layer import TwoLayerStore
from test_c1500_pool_contract import _isolated_slice_root

DAY = '2026-11-12'
ASOF = '2026-11-16T12:00:00+00:00'
BASE = Path(__file__).parent / 'fixtures/real/c1147'
RAW = (BASE / 'limit_up_pool_1112_formal.json').read_bytes()
PROVENANCE = json.loads((BASE / 'PROVENANCE.json').read_text())
if hashlib.sha256(RAW).hexdigest() != PROVENANCE['limit_up_pool_1112_formal.json']['sha256']:
    raise RuntimeError('fixture_digest_mismatch')

class Redis:
    def __init__(self, raw=None, fail=False, winner=None):
        self.raw, self.fail, self.winner, self.writes = raw, fail, winner, 0
    def get(self, key):
        if self.fail: raise ConnectionError('offline')
        return self.raw
    def set(self, key, raw, **kwargs):
        if not kwargs.get('nx'): raise AssertionError('not_conditional')
        self.writes += 1
        if self.winner is not None:
            self.raw = self.winner
            return False
        if self.raw is not None: return False
        self.raw = raw
        return True

class Cold:
    def __init__(self, entries=()):
        self.entries = {}
        for raw in entries:
            value = json.loads(raw)
            cold_archive.write_envelope(self, 'limit_up_pool', DAY, value['version'], raw, {})
    def write(self, dataset, day, version, rows, *, meta):
        self.entries.setdefault(version, []).extend(rows)
    def list_versions(self, dataset, day): return list(self.entries)
    def read(self, dataset, day, version): return self.entries.get(version, []).copy()

class BrokenPhysical:
    def read_versions(self, day): return []
    def read_day_rows(self, day):
        row = json.loads(RAW)['rows'][0]
        return {row['symbol']: dict(row, version='broken')}

class Contract(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(pool, '_td_store', side_effect=RuntimeError('offline')),
                        patch('engine_core.data_pipeline.redis_day_view.hot_view_expire_at', return_value=2000000000)]
        for p in self.patches: p.start(); self.addCleanup(p.stop)
    def test_corrupt_hot_cold_recoverable(self):
        redis = Redis(RAW[:1])  # Truncate the recorded envelope to reproduce corruption.
        store = TwoLayerStore(redis, Cold([RAW]))
        presence = store.presence('limit_up_pool', DAY, asof=ASOF)
        historical = store.get('limit_up_pool', DAY, asof=ASOF)
        guardian = resolve_presence('limit_up_pool', DAY, redis_client=redis, cold_store=Cold([RAW]))
        self.assertEqual(presence.status, 'CORRUPT')
        self.assertTrue(presence.metadata['cold_recoverable'])
        self.assertEqual(historical.status, 'CORRUPT')
        self.assertEqual(historical.rows, json.loads(RAW)['rows'])
        self.assertTrue(historical.metadata['cold_recoverable'])
        self.assertEqual(guardian, 'CORRUPT')
        self.assertIn('cold_recoverable', guardian.reason)
        self.assertEqual(redis.writes, 0)

    def test_corrupt_hot_without_cold_is_corrupt(self):
        redis = Redis(RAW[:1])
        result = resolve_presence('limit_up_pool', DAY, redis_client=redis, cold_store=Cold())
        self.assertEqual(result, 'CORRUPT')
        self.assertEqual(result.reason, 'redis_corrupt')
        self.assertEqual(redis.writes, 0)

    def test_guardian_registry_reports_both_facts(self):
        from datetime import datetime
        from engine_core.guardian import registry
        redis = Redis(RAW[:1])
        with patch('engine_core.guardian.presence.resolve_presence',
                   side_effect=lambda dataset, day: resolve_presence(
                       dataset, day, redis_client=redis, cold_store=Cold([RAW]))):
            items = registry.stale_items(datetime.fromisoformat('2026-11-16T12:00:00+00:00'),
                                         {'datasets': {}}, prev_trade_date=DAY)
        result = next(item for item in items if item['id'] == 'limit_up_pool')
        self.assertEqual(result['status'], 'CORRUPT')
        self.assertEqual(result['reason'], 'redis_corrupt;cold_degraded;cold_recoverable')
        self.assertEqual(redis.writes, 0)

    def test_missing_read_only_is_recoverable_not_restored(self):
        redis = Redis()
        store = TwoLayerStore(redis, Cold([RAW]))
        presence = store.presence('limit_up_pool', DAY, asof=ASOF)
        historical = store.get('limit_up_pool', DAY, asof=ASOF)
        self.assertEqual(presence.status, 'DEGRADED')
        self.assertTrue(presence.metadata['cold_recoverable'])
        self.assertEqual(historical.status, 'DEGRADED')
        self.assertTrue(historical.metadata['cold_recoverable'])
        self.assertEqual(redis.writes, 0)

    def test_hot_presence(self):
        self.assertEqual(resolve_presence('limit_up_pool', DAY, redis_client=Redis(RAW), cold_store=Cold()), 'PRESENT_REDIS')
    def test_read_only_presence(self):
        redis = Redis(RAW)
        result = TwoLayerStore(redis, Cold()).presence('limit_up_pool', DAY, asof=ASOF)
        self.assertEqual((result.status, redis.writes), ('PRESENT_REDIS', 0))
    def test_partial(self):
        v = json.loads(RAW); v['quality']['status'] = v['business_status'] = 'PARTIAL'
        result = TwoLayerStore(Redis(), Cold([json.dumps(v).encode()])).get('limit_up_pool', DAY, asof=ASOF)
        self.assertEqual(result.status, 'PARTIAL'); self.assertEqual(result.metadata['quality']['status'], 'PARTIAL')
    def test_corrupt_physical(self):
        rows, meta = pool.read(DAY, ASOF, redis_client=Redis(), td_store=BrokenPhysical(), cold_store=Cold())
        self.assertEqual((len(rows), meta['status']), (0, 'CORRUPT'))
    def test_redis_failure(self):
        result = TwoLayerStore(Redis(fail=True), Cold([RAW])).get('limit_up_pool', DAY, asof=ASOF)
        self.assertEqual(len(result.rows), len(json.loads(RAW)['rows']))
    def test_older_visible_and_corrupt_version(self):
        v = json.loads(RAW); v['version'] += '-future'; v['available_at'] = '2099-01-01T00:00:00+00:00'
        cold = Cold([RAW, json.dumps(v).encode()]); cold.entries['corrupt'] = []
        rows, meta = pool.read(DAY, ASOF, redis_client=Redis(), cold_store=cold)
        self.assertEqual(len(rows), len(json.loads(RAW)['rows'])); self.assertTrue(meta['selection_uncertain'])
    def test_physical_recovery(self):
        value = json.loads(RAW)
        class Physical:
            def read_versions(self, day): return [value]
            def read_day_rows(self, day):
                return {r['symbol']: dict(r, version=value['version']) for r in value['rows']}
        redis = Redis()
        with patch.object(pool, '_td_store', return_value=Physical()):
            result = TwoLayerStore(redis, Cold()).restore('limit_up_pool', DAY)
        self.assertEqual((redis.writes, result.status), (1, 'RESTORED_FROM_TD'))
    def test_nx_winner(self):
        v = json.loads(RAW); v['quality']['status'] = v['business_status'] = 'PARTIAL'
        redis = Redis(winner=json.dumps(v).encode())
        result = TwoLayerStore(redis, Cold([RAW])).restore('limit_up_pool', DAY)
        self.assertEqual((redis.writes, result.status, result.metadata['storage_source']), (1, 'PARTIAL', 'redis'))

if __name__ == '__main__': unittest.main(verbosity=2)
