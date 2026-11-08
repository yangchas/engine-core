import json
from pathlib import Path

from engine_core.data_pipeline.adjust_factor import factor_for


def test_factor_for_event_boundaries():
    records = json.loads((Path(__file__).parent / 'fixtures/adjust_factor_c846/events.json').read_text())
    record = next(record for record in records if record['code'] == 'sh.600519')
    events = [dict(zip(record['fields'], row)) for row in record['rows']]
    assert factor_for(events, '2025-06-26') == (0.960527, 'FORMAL')
    assert factor_for(events, '2025-06-25') == (0.942063, 'FORMAL')
    assert factor_for(events, '2000-01-01') == (None, 'BEFORE_FIRST_EVENT')


def test_store_replaces_before_deleting_stale_dates():
    from engine_core.data_pipeline.adjust_factor import AdjustFactorStore
    records = json.loads((Path(__file__).parent / 'fixtures/adjust_factor_c846/events.json').read_text())
    record = next(r for r in records if r['code'] == 'sh.600519')
    rows = [dict(zip(record['fields'], r)) for r in record['rows']]

    class TD:
        database = 'core_data'
        def __init__(self):
            self.sql = []
        def _run(self, sql, **kwargs):
            self.sql.append(sql)
            if sql.startswith('SELECT'):
                row = rows[0]
                return [(row['dividOperateDate'], row['foreAdjustFactor'], row['backAdjustFactor'], row['adjustFactor'], '20260101000000-aaaaaaaa')]
            return []
    td = TD()
    store = AdjustFactorStore(td)
    store.replace_snapshot('600519', rows[1:3], '20261108000000-bbbbbbbb')
    writes = [s for s in td.sql if s.startswith(('INSERT', 'DELETE'))]
    assert writes[0].startswith('INSERT')
    assert writes[-1].startswith('DELETE')
    assert rows[0]['dividOperateDate'] not in [r['dividOperateDate'] for r in rows[1:3]]
