# DATA: real:td/errors/query_memory_exhausted_0x073a.json (errno and text of the real TD error the 01:00 backfill got)
"""C694: when a TD statement fails, the error names the statement - 'Query memory exhausted' alone cannot say which."""
import json
from pathlib import Path

import pytest

from engine_core.data_pipeline.td_sink import TaosStore, TdResourceExhausted

REAL = json.loads((Path(__file__).parent / "fixtures" / "real" / "td" / "errors"
                   / "query_memory_exhausted_0x073a.json").read_text(encoding="utf-8"))


class _Failing:
    def cursor(self):
        return self

    def execute(self, sql):
        class ProgrammingError(Exception):
            pass
        error = ProgrammingError(REAL["message"])
        error.errno = int(REAL["errno"], 16)
        raise error

    def close(self):
        pass


def test_the_failed_statement_is_named_in_the_error():
    store = TaosStore("core_dev_c694", connect=lambda: _Failing())
    sql = ("SELECT symbol, LAST(ts) FROM core_dev_c694.daily_kline_v1\n   GROUP BY symbol")
    with pytest.raises(TdResourceExhausted) as raised:
        store._run(sql)
    text = str(raised.value)
    assert "[0x073a]: Query memory exhausted" in text and "sql=SELECT symbol, LAST(ts) FROM core_dev_c694.daily_kline_v1 GROUP BY symbol" in text
    assert raised.value.error_code == "TD_RESOURCE_EXHAUSTED" and raised.value.retryable


def test_a_long_statement_is_cut_short():
    store = TaosStore("core_dev_c694", connect=lambda: _Failing())
    with pytest.raises(TdResourceExhausted) as raised:
        store._run("SELECT " + ",".join(f"c{i}" for i in range(500)))
    assert len(str(raised.value)) < 500
