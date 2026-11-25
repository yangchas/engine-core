import json
from pathlib import Path

import pytest

from engine_core.data_pipeline.providers.wencai_hot_rank import normalize

FIXTURE = Path(__file__).parent / 'fixtures/real/c1223/2026-11-13.json'


def test_real_historical_response_and_wrong_date():
    payload = json.loads(FIXTURE.read_text())['payload']
    rows = normalize(payload, '2026-11-13')
    assert len(rows) == 100
    assert [r['rank'] for r in rows] == list(range(1, 101))
    assert all(r['trade_date'] == '2026-11-13' and r['source'] == 'wencai_hot_rank' for r in rows)
    with pytest.raises(ValueError):
        normalize(payload, '2026-11-12')
