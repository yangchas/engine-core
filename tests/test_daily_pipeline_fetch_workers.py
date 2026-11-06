import threading
import time

from engine_core import daily_pipeline, live_session


def test_default_fetches_checkpoints_serially(monkeypatch):
    lock = threading.Lock()
    active = 0
    maximum = 0
    calls = []

    def fake_states_at(connection, cutoff):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            calls.append(cutoff.strftime('%H:%M:%S'))
        time.sleep(0.03)
        with lock:
            active -= 1
        return {'600001': {'pct_change': 1.0, 'match_amount_yuan': 10, 'amt_yuan': 20}}

    monkeypatch.setattr(live_session, 'states_at', fake_states_at)
    rows = daily_pipeline._live_section_stocks(
        object(), trade_date='2026-10-15', stage='open', asof='09:45:30',
        relations={}, plate_names={}, stock_names={}, history_path=None)
    assert maximum == 1
    assert calls == ['09:24:10', '09:25:06', '09:32:00', '09:35:00',
                     '09:38:00', '09:40:00', '09:45:30']
    assert rows[0]['p0925_pct'] == 1.0
    assert rows[0]['p0945_amount_yuan'] == 20


def test_explicit_workers_enable_parallel_fetch(monkeypatch):
    lock = threading.Lock()
    active = 0
    maximum = 0
    calls = []

    def fake_states_at(connection, cutoff):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            calls.append(cutoff.strftime('%H:%M:%S'))
        time.sleep(0.03)
        with lock:
            active -= 1
        return {'600001': {'pct_change': 1.0, 'match_amount_yuan': 10, 'amt_yuan': 20}}

    monkeypatch.setattr(live_session, 'states_at', fake_states_at)
    rows = daily_pipeline._live_section_stocks(
        object(), trade_date='2026-10-15', stage='open', asof='09:45:30',
        relations={}, plate_names={}, stock_names={}, history_path=None, max_fetch_workers=3)
    assert 1 < maximum <= 3
    assert sorted(calls) == ['09:24:10', '09:25:06', '09:32:00', '09:35:00',
                             '09:38:00', '09:40:00', '09:45:30']
    assert rows[0]['p0925_pct'] == 1.0
    assert rows[0]['p0945_amount_yuan'] == 20


def test_live_pages_default_fetch_workers():
    import inspect
    from engine_core.live_pages import render_page

    assert inspect.signature(render_page).parameters['max_fetch_workers'].default == 1
