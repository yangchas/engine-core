import json
from pathlib import Path

import pytest

from engine_core.data_pipeline.providers import wencai_hot_rank

FIXTURE = Path(__file__).parent / 'fixtures/real/c1223/2026-11-13.json'


def test_fetch_real_response_and_reject_wrong_date():
    payload = json.loads(FIXTURE.read_text())['payload']
    calls = []
    def query(**kwargs):
        calls.append(kwargs)
        return {'payload': payload}
    rows = wencai_hot_rank.fetch('2026-11-13', cookie_file=None, query_fn=query)
    assert calls[0]['query'] == '2026-11-13个股热度排名前100'
    assert [row['rank'] for row in rows] == list(range(1, 101))
    with pytest.raises(ValueError, match='wencai_hot_rank_invalid_row_or_date'):
        wencai_hot_rank.fetch('2026-11-12', cookie_file=None, query_fn=query)


def test_real_response_published_and_read_back(tmp_path, monkeypatch):
    from engine_core.data_pipeline import cold_archive, hot_rank_view
    from engine_core.data_pipeline.jobs import JobRunner, RequestBudget
    from engine_core.storage.two_layer import TwoLayerStore

    class Redis:
        def __init__(self): self.values = {}
        def set(self, key, value, **kwargs): self.values[key] = value; return True
        def get(self, key): return self.values.get(key)
    redis = Redis()
    archived = []
    monkeypatch.setattr(cold_archive, 'archive', lambda *args: archived.append(args) or True)
    monkeypatch.setattr(cold_archive, 'flush', lambda: None)
    monkeypatch.setattr(hot_rank_view, 'hot_view_expire_at', lambda day: 2000000000)
    payload = json.loads(FIXTURE.read_text())['payload']
    runner = JobRunner(artifact_root=tmp_path, redis=redis, budget=RequestBudget(min_interval_seconds=0))
    result = runner.run('hot_rank', '2026-11-13',
                        fetch=lambda day: wencai_hot_rank.fetch(day, cookie_file=None, query_fn=lambda **kw: {'payload': payload}),
                        source='wencai_hot_rank', publish=True)
    read = TwoLayerStore(redis).get('hot_rank', '2026-11-13')
    assert read.status == 'PRESENT_REDIS'
    assert [row['rank'] for row in read.rows] == list(range(1, 101))
    assert result.manifest['version'].startswith('wencai-')
    assert archived and archived[0][2] == result.manifest['version']
