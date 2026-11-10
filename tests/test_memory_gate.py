from engine_core.guardian.memory_gate import admit, available_mb, required_mb
from examples import guardian_handler


def test_required_budget_and_override():
    assert [required_mb({'cost': cost}) for cost in ('light', 'medium', 'heavy', 'unknown')] == [0, 1200, 1800, 0]
    assert required_mb({'cost': 'heavy', 'args': {'min_available_mb': 123}}) == 123
    assert required_mb({'cost': 'heavy', 'args': {'min_available_mb': 0}}) == 0


def test_admit_low_memory(tmp_path):
    meminfo = tmp_path / 'meminfo'
    meminfo.write_text('MemTotal: 5000000 kB\nMemAvailable: 1024000 kB\n')
    assert available_mb(meminfo) == 1000
    allowed, info = admit({'cost': 'heavy'}, reader=lambda: 1000)
    assert not allowed
    assert info == {'available_mb': 1000, 'required_mb': 1800, 'cost': 'heavy'}


def test_run_low_memory_does_not_dispatch(monkeypatch):
    monkeypatch.setattr(guardian_handler, 'admit', lambda request: (False, {'available_mb': 1000, 'required_mb': 1800, 'cost': 'heavy'}))
    monkeypatch.setattr(guardian_handler, '_run_event', lambda request: (_ for _ in ()).throw(AssertionError('dispatched')))
    code, reply = guardian_handler._run({'cost': 'heavy', 'handler': 'anything'})
    assert code == 75
    assert reply == {'contract': 1, 'status': 'failed', 'retryable': True, 'error_code': 'TD_RESOURCE_EXHAUSTED', 'artifacts': [], 'detail': {'reason': 'LOW_MEMORY', 'available_mb': 1000, 'required_mb': 1800, 'cost': 'heavy'}}


def test_run_admitted_dispatches(monkeypatch):
    monkeypatch.setattr(guardian_handler, 'admit', lambda request: (True, {}))
    expected = (0, {'contract': 1, 'status': 'ok'})
    monkeypatch.setattr(guardian_handler, '_event_from_request', lambda request: (None, 'event'))
    monkeypatch.setattr(guardian_handler, '_run_event', lambda request: expected)
    assert guardian_handler._run({'cost': 'heavy', 'handler': 'anything'}) == expected


def test_dry_run_skips_admission(monkeypatch):
    monkeypatch.setattr(guardian_handler, 'admit', lambda request: (_ for _ in ()).throw(AssertionError('admission called')))
    monkeypatch.setattr(guardian_handler, '_event_from_request', lambda request: (None, 'event'))
    monkeypatch.setattr(guardian_handler, '_run_event', lambda request: (0, {'status': 'ok'}))
    assert guardian_handler._run({'dry_run': True}) == (0, {'status': 'ok'})
    monkeypatch.setenv('GUARDIAN_DRY_RUN', '1')
    assert guardian_handler._run({}) == (0, {'status': 'ok'})


def test_event_override_without_request_cost():
    allowed, info = admit({'event': 'open_report_0932'}, reader=lambda: 2000)
    assert allowed
    assert info['required_mb'] == 1800
    assert info['cost_source'] == 'override'


def test_schedule_heavy_event():
    allowed, info = admit({'event': 'night_recap'}, reader=lambda: 2000)
    assert allowed
    assert info['required_mb'] == 1800
    assert info['cost_source'] == 'schedule'


def test_missing_schedule_defaults_to_light(monkeypatch, tmp_path):
    from engine_core.guardian import memory_gate
    monkeypatch.setattr(memory_gate, '_SCHEDULE_PATH', tmp_path / 'missing.json')
    allowed, info = admit({'event': 'night_recap'}, reader=lambda: 0)
    assert allowed
    assert info['required_mb'] == 0
    assert info['cost_source'] == 'default'


def test_args_override_event_budget():
    allowed, info = admit({'event': 'open_report_0932', 'args': {'min_available_mb': 123}}, reader=lambda: 123)
    assert allowed
    assert info['required_mb'] == 123
    assert info['cost_source'] == 'args'
