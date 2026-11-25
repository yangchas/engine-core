import pytest

from examples.startup_dryrun import ReadOnlyRedis, ReadOnlyTD


class RecordingClient:
    def __init__(self):
        self.calls = []

    def execute_command(self, *args, **kwargs):
        self.calls.append(args)
        return None

    def execute(self, sql, *args, **kwargs):
        self.calls.append((sql,))
        return None


def test_clients_reject_dangerous_commands_before_delegation():
    redis_client, td_cursor = RecordingClient(), RecordingClient()
    violations = []
    redis = ReadOnlyRedis(redis_client, violations)
    td = ReadOnlyTD(td_cursor, violations)
    redis.execute_command('GET', 'core:readonly')
    td.execute('SELECT 1')
    for command in ('SET', 'DEL', 'HSET'):
        with pytest.raises(PermissionError):
            redis.execute_command(command, 'core:readonly', 'value')
    for sql in ('INSERT INTO x VALUES (1)', 'CREATE TABLE x (x INT)'):
        with pytest.raises(PermissionError):
            td.execute(sql)
    assert redis_client.calls == [('GET', 'core:readonly')]
    assert td_cursor.calls == [('SELECT 1',)]
    assert len(violations) == 5


def test_data_root_guard_rejects_writes_and_records_violations(tmp_path):
    from examples.startup_dryrun import protect_data_root

    root = tmp_path / 'data'
    root.mkdir()
    existing = root / 'calendar.json'
    existing.write_text('original')
    violations = []
    with protect_data_root(root, violations):
        assert existing.read_text() == 'original'
        with pytest.raises(PermissionError):
            existing.write_text('changed')
        with pytest.raises(PermissionError):
            (root / 'new-dir').mkdir()
    assert existing.read_text() == 'original'
    assert len(violations) == 2
    assert all(row['backend'] == 'filesystem' for row in violations)


def test_rehearsal_plans_from_real_calendar_and_records_denied_writes(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    root = tmp_path / 'core-data'
    (root / 'trade_calendar').mkdir(parents=True)
    fixture_dir = Path(__file__).parent / 'fixtures' / 'startup_dryrun_c1217'
    (root / 'trade_calendar' / 'calendar.json').write_bytes(
        (fixture_dir / 'calendar.json').read_bytes())
    (root / 'catalog.json').write_text('{"datasets": {}}')
    result = subprocess.run(
        [sys.executable, 'examples/startup_dryrun.py', '--date', '2026-11-13',
         '--now', '08:00', '--root', str(root)],
        capture_output=True, text=True, check=True,
    )
    report = json.loads(result.stdout)
    assert list(report['boot']) == ['boot_trade_day', 'boot_data_health', 'boot_reconcile']
    assert report['boot']['boot_trade_day']['detail']['is_trading_day'] is True
    assert isinstance(report['plan'], list)
    assert all('item' in row and 'action' in row and 'reason' in row for row in report['plan'])
    assert report['violations'] == []


def test_installed_client_guards_stop_writes_before_delegation(monkeypatch):
    import redis
    from engine_core.data_pipeline import td_sink
    from examples.startup_dryrun import guarded_clients

    redis_client, td_cursor = RecordingClient(), RecordingClient()

    class Connection:
        def cursor(self):
            return td_cursor

    monkeypatch.setattr(redis.Redis, 'from_url', lambda *args, **kwargs: redis_client)
    monkeypatch.setattr(td_sink, 'connect_td', lambda *args, **kwargs: Connection())
    violations = []
    with guarded_clients(violations):
        with pytest.raises(PermissionError):
            redis.Redis.from_url('unused').set('core:readonly', 'value')
        with pytest.raises(PermissionError):
            td_sink.connect_td().cursor().execute('INSERT INTO x VALUES (1)')
    assert redis_client.calls == []
    assert td_cursor.calls == []
    assert [row['backend'] for row in violations] == ['redis', 'td']
