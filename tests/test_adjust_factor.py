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
