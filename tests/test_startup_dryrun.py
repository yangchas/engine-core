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
