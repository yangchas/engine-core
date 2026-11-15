import json
import re
from pathlib import Path

from engine_core.data_pipeline.providers.limit_up_today_sources import publish_hot_snapshot


def test_real_legacy_view_republication_normalizes_times(monkeypatch):
    from engine_core.data_pipeline import redis_day_view
    fixture = Path(__file__).parent / 'fixtures/real/c1120'
    pointer = json.loads((fixture / 'pointer.json').read_text())
    old = json.loads((fixture / 'version.json').read_text())
    class Replay:
        def __init__(self):
            self.values = {f"core:cache:data:limit_up_pool_today:{pointer['trade_date']}": json.dumps(pointer)}
        def get(self, key): return self.values.get(key)
        def set(self, key, value, exat): self.values[key] = value
    client = Replay()
    monkeypatch.setattr(redis_day_view, 'hot_view_expire_at', lambda _: 4102444800)
    manifest = {k: v for k, v in old.items() if k != 'rows'}
    # Exercise a seconds-only candidate against the real millisecond legacy pointer.
    from datetime import datetime, timezone
    sample = datetime.fromisoformat(manifest["as_of"])
    manifest["as_of"] = sample.isoformat(timespec="seconds")
    manifest["fetched_at"] = sample.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest["declared_available_at"] = sample.replace(tzinfo=None).isoformat()
    result = publish_hot_snapshot(manifest, old['rows'], client=client)
    assert result['status'] == 'published'
    record = json.loads(client.values[result['keys'][0]])
    new_pointer = json.loads(client.values[result['keys'][1]])
    assert set(('schema_version', 'dataset', 'trade_date', 'source', 'version', 'captured_at', 'available_at', 'row_count', 'rows')) <= record.keys()
    assert record['row_count'] == len(record['rows'])
    pattern = r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+08:00$'
    for key in ('captured_at', 'available_at', 'fetched_at', 'declared_available_at', 'as_of'):
        if record.get(key) is not None:
            assert re.fullmatch(pattern, record[key]), (key, record[key])
    assert re.fullmatch(pattern, new_pointer['as_of'])
    assert record["fetched_at"] == record["declared_available_at"] == new_pointer["as_of"]
    for row in record['rows']:
        for key in ('as_of', 'source_ts'):
            if row.get(key) is not None:
                assert re.fullmatch(pattern, row[key]), (key, row[key])
        for key in ('first_limit_time', 'final_limit_time'):
            if row.get(key) is not None:
                assert re.fullmatch(r'\d\d:\d\d:\d\d', row[key])
