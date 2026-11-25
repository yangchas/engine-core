# DATA: none (behavioural contract on a call-recording stand-in connection; no data rows involved)
"""C678: every TD statement's cursor is closed as soon as its rows are read.

A taos query handle keeps the query registered (and its memory held) until the cursor is closed or
runs another statement; relying on refcount GC to do that is not a contract.
"""
import pytest

from engine_core.data_pipeline.daily_dde import TaosDdeStore
from engine_core.data_pipeline.limit_pool_td import PoolTaosStore
from engine_core.data_pipeline.reference_snapshot_td import ReferenceSnapshotStore
from engine_core.data_pipeline.td_ladder_sink import TaosLadderStore
from engine_core.data_pipeline.td_sink import TaosStore


class _Cursor:
    def __init__(self, log, fail):
        self.log, self.fail, self.closed = log, fail, False
        log.append(self)

    def execute(self, sql):
        if self.fail:
            raise RuntimeError("boom")

    def fetchall(self):
        return [("core_dev_c678",)]       # every listing names the database, so schema_exists goes on to its later statements

    def fetchone(self):
        return None

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


@pytest.mark.parametrize("store_class", [TaosStore, TaosLadderStore, PoolTaosStore])
def test_schema_exists_closes_every_cursor_it_opens(store_class):
    """C688: SHOW DATABASES was the one statement that kept its cursor, so every handler that asked whether the schema
    exists left a finished query registered in TD for as long as its connection lived (a 33 minute probe, a 72 minute
    backfill - both looked 'hung' in SHOW QUERIES with sub_status SUCCEED)."""
    conn = _Conn()
    store = store_class("core_dev_c678", connect=lambda: conn)
    assert store.schema_exists() is True
    assert len(conn.cursors) >= 2 and all(cursor.closed for cursor in conn.cursors)


def test_reference_snapshot_store_closes_every_cursor_it_opens():
    conn = _Conn()
    store = ReferenceSnapshotStore("core_dev_c678", connect=lambda: conn)
    store.begin("plate_registry", source="test", available_at="2026-11-05T09:00:00+08:00")
    assert store.read_latest("plate_registry") is None
    assert len(conn.cursors) >= 4 and all(cursor.closed for cursor in conn.cursors)


@pytest.mark.parametrize("fail", [False, True])
def test_reference_snapshot_store_closes_its_cursor_when_the_statement_fails(fail):
    conn = _Conn(fail=fail)
    store = ReferenceSnapshotStore("core_dev_c678", connect=lambda: conn)
    try:
        store._run("SELECT 1", fetch="all")
    except RuntimeError:
        assert fail
    assert len(conn.cursors) == 1 and conn.cursors[0].closed
