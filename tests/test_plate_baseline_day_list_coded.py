# DATA: real:td/errors/query_memory_exhausted_0x073a.json (errno and text of the real TD error); the connection only raises it
"""C708: the day-list statement of the plate amount baseline reports a TD failure by its code, not as INTERNAL_ERROR."""
import json
from pathlib import Path

import pytest
from taos.error import ProgrammingError

from engine_core import plate_amount_baseline as module
from engine_core.data_pipeline.td_sink import TdResourceExhausted

REAL = json.loads((Path(__file__).parent / "fixtures" / "real" / "td" / "errors"
                   / "query_memory_exhausted_0x073a.json").read_text(encoding="utf-8"))


class _Failing:
    def cursor(self):
        return self

    def execute(self, sql):
        self.sql = sql
        raise ProgrammingError(REAL["message"], int(REAL["errno"], 16))

    def close(self):
        pass


def test_day_list_memory_exhaustion_is_a_coded_error(monkeypatch, tmp_path):
    connection = _Failing()
    monkeypatch.setattr(module, "_td_connect", lambda: connection)
    with pytest.raises(TdResourceExhausted) as raised:
        module.build_and_store_plate_amount_baseline("2026-11-06", artifact_root=tmp_path, publish=False)
    assert raised.value.error_code == "TD_RESOURCE_EXHAUSTED" and "plate_amount_baseline_day_list" in str(raised.value)
    assert connection.sql == ('SELECT DISTINCT trade_date FROM market_data1.auction_snapshot_v2 '
                              'WHERE trade_date >= "20260619" AND trade_date < "20261106" AND auction_tag="0925"')
