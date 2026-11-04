# DATA: none (behavioural contract on a call-recording stand-in connection; no data rows involved)
"""C678: every TD statement's cursor is closed as soon as its rows are read.

A taos query handle keeps the query registered (and its memory held) until the cursor is closed or
runs another statement; relying on refcount GC to do that is not a contract.
"""
import pytest

from engine_core.data_pipeline.daily_dde import TaosDdeStore
from engine_core.data_pipeline.td_sink import TaosStore


class _Cursor:
    def __init__(self, log, fail):
        self.log, self.fail, self.closed = log, fail, False
        log.append(self)

    def execute(self, sql):
        if self.fail:
            raise RuntimeError("boom")

    def fetchall(self):
        return [("x",)]

    def close(self):
        self.closed = True


class _Conn:
    def __init__(self, fail=False):
        self.cursors, self.fail = [], fail

    def cursor(self):
        return _Cursor(self.cursors, self.fail)

    def close(self):
        pass


@pytest.mark.parametrize("make", [
    lambda conn: TaosStore("core_dev_c678", connect=lambda: conn),
    lambda conn: TaosDdeStore("core_dev_c678", connect=lambda: conn),
])
@pytest.mark.parametrize("fail", [False, True])
def test_run_closes_its_cursor(make, fail):
    conn = _Conn(fail=fail)
    store = make(conn)
    try:
        store._run("SELECT 1")
    except Exception:
        assert fail
    assert len(conn.cursors) == 1 and conn.cursors[0].closed
