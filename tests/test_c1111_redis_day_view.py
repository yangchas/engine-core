import json
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from engine_core import auction_panorama, auction_points, auction_stock_facts
from engine_core.data_pipeline import redis_day_view
from engine_core.data_pipeline.providers.limit_up_today_sources import publish_hot_snapshot


def test_four_writers_use_real_calendar_deadlines(monkeypatch):
    monkeypatch.setenv('CORE_DATA_ROOT', '/home/exedev/core-data')
    monkeypatch.setenv('REDIS_URL', 'redis://unused')
    fixture = Path(__file__).parent / 'fixtures/real/redis_auction_full_row_2026-11-12_1207.json'
    raw = json.loads(fixture.read_text())['rows']
    rows = [dict(row, symbol=symbol, source='redis_auction_full', auction_tag='0925')
            for symbol, row in raw.items()]
    client = Mock()
    client.get.return_value = None
    client.pipeline = None
    import redis
    monkeypatch.setattr(redis.Redis, 'from_url', lambda *a, **kw: client)
    for module in (auction_panorama, auction_stock_facts):
        monkeypatch.setattr(module.ArtifactCatalog, 'commit', lambda *a, **kw: {'version': 'v', 'sha256': 's'})
    tz = ZoneInfo('Asia/Shanghai')
    for day, now, expected in [
        ('2026-11-10', '2026-11-10T09:00:00+08:00', '2026-11-17T00:00:00+08:00'),
        ('2026-11-13', '2026-11-13T09:00:00+08:00', '2026-11-20T00:00:00+08:00'),
        ('2026-11-10', '2026-11-16T12:00:00+08:00', '2026-11-17T12:00:00+08:00'),
    ]:
        monkeypatch.setattr(redis_day_view, '_now_timestamp', lambda: datetime.fromisoformat(now).timestamp(), raising=False)
        facts = auction_stock_facts.build_auction_stock_facts(day, day + 'T09:26:00+08:00', rows, [])
        product = auction_panorama.build_auction_panorama(facts, plate_members=[], yesterday_pool=[])
        manifest = {'trade_date': day, 'as_of': day + 'T09:26:00+08:00', 'version': 'v', 'sha256': 's'}
        client.set.reset_mock()
        publish_hot_snapshot(manifest, rows, client=client)
        auction_points._publish_redis(day, manifest, rows)
        auction_stock_facts.store_auction_stock_facts(facts, redis=client)
        auction_panorama.store_auction_panorama(product, redis=client)
        calls = client.set.call_args_list
        assert len(calls) == 5
        assert all(call.kwargs.get('exat') == int(datetime.fromisoformat(expected).astimezone(tz).timestamp()) for call in calls)
