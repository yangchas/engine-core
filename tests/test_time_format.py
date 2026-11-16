import json
from pathlib import Path
from datetime import datetime
import pytest

SAMPLES = json.loads((Path(__file__).parent / 'fixtures/real/c1302/timestamps.json').read_text())
POOL = SAMPLES['core:view:limit_up_pool:2026-11-13']['fields']['available_at']
HOT = SAMPLES['core:view:hot_rank:2026-11-16']['fields']
CHECKPOINT = SAMPLES['core:view:intraday_checkpoint:2026-11-16:09:45:00']['fields']


def test_real_timestamps_and_pit_ceiling():
    from engine_core.data_pipeline.time_format import shanghai_seconds, shanghai_seconds_ceil
    assert shanghai_seconds(POOL) == '2026-11-15T17:20:05+08:00'
    visible = shanghai_seconds_ceil(POOL)
    assert visible == '2026-11-15T17:20:06+08:00'
    assert datetime.fromisoformat(visible) > datetime.fromisoformat('2026-11-15T17:20:05+08:00')
    assert datetime.fromisoformat(visible) <= datetime.fromisoformat('2026-11-15T17:20:06+08:00')
    assert shanghai_seconds_ceil(visible) == visible
    assert shanghai_seconds(None) is None
    with pytest.raises(ValueError): shanghai_seconds('2026-11-16T09:25:03')


def test_real_hot_rank_publish_pit_and_old_new_reader(monkeypatch):
    from engine_core.data_pipeline import hot_rank_view as view
    class Redis:
        def set(self, key, value, **kwargs): self.raw = value; return True
        def get(self, key): return self.raw
    client = Redis()
    monkeypatch.setattr(view, 'hot_view_expire_at', lambda day: 1)
    monkeypatch.setattr(view.cold_archive, 'archive', lambda *a: None)
    metadata = {'version': 'real-timestamp', 'available_at': POOL, 'fetched_at': HOT['fetched_at'], 'committed_at': HOT['committed_at']}
    value, _ = view.publish(client, '2026-11-16', [SAMPLES['core:view:hot_rank:2026-11-16']['first_row']], metadata)
    assert value['available_at'] == '2026-11-15T17:20:06+08:00'
    assert value['fetched_at'] == '2026-11-16T08:31:06+08:00'
    assert value['committed_at'] == '2026-11-16T08:31:09+08:00'
    assert view.validate(client.raw, '2026-11-16')['available_at'] == value['available_at']
    from engine_core.storage.two_layer import TwoLayerStore
    store = TwoLayerStore(client)
    assert store.get('hot_rank', '2026-11-16', asof='2026-11-15T17:20:05+08:00').status == 'MISSING'
    assert store.get('hot_rank', '2026-11-16', asof='2026-11-15T17:20:06+08:00').status == 'PRESENT_REDIS'
    old = dict(value, available_at=POOL)
    assert view.validate(json.dumps(old), '2026-11-16')['available_at'] == POOL


def test_checkpoint_capture_normalizes_real_timestamp():
    from engine_core.data_pipeline.intraday_checkpoint import build_intraday_checkpoint
    p = build_intraday_checkpoint({'trade_date':'2026-11-16', 'cutoff':'09:45:00', 'tolerance_seconds':60, 'min_symbols':1, 'read_at':CHECKPOINT['manifest_captured_at']})
    assert p.manifest['captured_at'] == '2026-11-16T09:48:25+08:00'
