import json
from pathlib import Path

import pytest

from engine_core.data_pipeline.stock_listing import fetch, listed_on, run

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/stock_listing_c819/basic_subset.json').read_text())


class Result:
    fields = FIXTURE['fields']
    error_code = FIXTURE['error_code']
    error_msg = FIXTURE['error_msg']

    def __init__(self):
        self.rows = iter(FIXTURE['rows'])

    def next(self):
        self.row = next(self.rows, None)
        return self.row is not None

    def get_row_data(self):
        return self.row


class Client:
    def query_stock_basic(self, *, code):
        assert code == ''
        return Result()


def test_listed_on_boundaries():
    rows = fetch(Client())
    assert 'sz.301139' in listed_on(rows, '2026-10-27')
    assert 'sz.301139' not in listed_on(rows, '2026-10-28')
    assert 'sz.001381' not in listed_on(rows, '2026-11-02')
    assert 'sz.001381' in listed_on(rows, '2026-11-03')


class Store:
    def __init__(self):
        self.active = None
        self.writes = 0

    def read_latest(self, dataset):
        assert dataset == 'stock_listing'
        return self.active

    def begin(self, dataset, *, source, available_at):
        assert dataset == 'stock_listing'
        return 'version-1'

    def write_and_activate(self, dataset, version, rows, **kwargs):
        self.writes += 1
        self.active = {'version': version, 'rows': rows, 'available_at': kwargs['available_at']}
        return self.active


def test_run_writes_and_reuses():
    store = Store()
    rows = fetch(Client())
    code, reply = run('2026-11-08', store=store, fetcher=lambda: rows, min_rows=len(rows))
    assert code == 0 and reply['detail']['rows'] == len(rows)
    assert [{k: v for k, v in row.items() if k != 'trade_date'} for row in store.active['rows']] == rows
    assert store.writes == 1
    code, reply = run('2026-11-08', store=store, fetcher=lambda: rows, min_rows=len(rows))
    assert code == 20 and reply['error_code'] == 'ALREADY_SATISFIED'
    assert store.writes == 1


def test_run_rejects_too_few_rows():
    store = Store()
    code, reply = run('2026-11-08', store=store, fetcher=lambda: fetch(Client()))
    assert reply['status'] == 'failed' and code != 0
    assert store.active is None and store.writes == 0


@pytest.mark.parametrize(('message', 'error_code'), [
    ('baostock_login_failed', 'BAOSTOCK_LOGIN_FAILED'),
    ('baostock_stock_basic_failed', 'BAOSTOCK_RANGE_FAILED'),
])
def test_run_maps_baostock_failures(message, error_code):
    def fail():
        raise RuntimeError(message)

    code, reply = run('2026-11-08', store=Store(), fetcher=fail)
    assert code == 75 and reply['error_code'] == error_code


def test_run_maps_baostock_login_exception(monkeypatch):
    import sys
    from types import SimpleNamespace

    def fail():
        raise ConnectionError('baostock.login unavailable')

    monkeypatch.setitem(sys.modules, 'baostock', SimpleNamespace(login=fail))
    code, reply = run('2026-11-06', store=Store())
    assert code == 75 and reply['error_code'] == 'BAOSTOCK_LOGIN_ERROR'
