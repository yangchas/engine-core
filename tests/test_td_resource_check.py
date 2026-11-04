# DATA: real:td/show_queries.json.gz (SHOW QUERIES recorded 2026-11-04 with the client's real command line); derived variant: the same answer with no rows
"""The check names whoever holds TD's query memory: a long query of another process is 'degraded', never silent."""
import pytest

from engine_core.guardian import td_resource_check as check
from engine_core.guardian.contract import check_reply
from tests.memory_redis import MemoryRedis
from tests.replay import ReplayTd, load_recording

RECORDING = load_recording("show_queries")
OWNERS = RECORDING["call"]["owners"]
SQL = RECORDING["statements"][0]["sql"]


def owner_of(pid):
    return OWNERS.get(str(pid), "")


def connection(rows=None):
    """Replay of the recorded answer; ``rows=[]`` is the derived 'nothing is running' variant."""
    replay = ReplayTd("show_queries")
    if rows is not None:
        replay.entries[SQL] = {**replay.entries[SQL], "rows": rows}
    return replay


def test_the_recorded_long_query_is_found_with_its_owner_and_kill_id():
    found = check.long_queries(connection().cursor(), threshold_seconds=1800, owner_of=owner_of)
    assert len(found) == 1
    query = found[0]
    assert query["sql"].startswith("select symbol, last(ts) as latest_ts from daily_factors")
    assert "engine_next.app_main" in query["owner"] and query["owner_is_core"] is False
    assert query["kill_id"] and query["minutes"] > 30


def test_a_query_shorter_than_the_threshold_is_not_reported():
    assert check.long_queries(connection().cursor(), threshold_seconds=10 ** 9, owner_of=owner_of) == []


def test_run_reports_a_foreign_long_query_as_degraded_with_a_valid_reply():
    redis = MemoryRedis()
    from engine_core.runtime_state import StateStore
    code, reply = check.run({"args": {}}, ".", connect_fn=connection, owner_of=owner_of, state=StateStore(redis))
    assert (code, reply["status"], reply["error_code"]) == (10, "degraded", "TD_FOREIGN_LONG_QUERY")
    assert reply["detail"]["foreign"] == 1 and check_reply(code, reply) == []
    assert 0 < redis.ttl(check.REDIS_KEY) <= check.REDIS_TTL_SECONDS            # the latest result is readable in Redis


def test_run_is_ok_when_nothing_long_is_running():
    code, reply = check.run({"args": {}}, ".", connect_fn=lambda: connection(rows=[]), owner_of=owner_of)
    assert (code, reply["status"]) == (0, "ok") and reply["detail"]["long_queries"] == []
    assert check_reply(code, reply) == []


def test_a_long_query_of_core_itself_is_counted_as_core_not_foreign():
    own = lambda pid: "/usr/bin/python /home/exedev/repos/engine_core-live/examples/guardian_handler.py run"   # noqa: E731
    code, reply = check.run({"args": {}}, ".", connect_fn=connection, owner_of=own)
    assert code == 0 and reply["detail"]["core"] == 1 and reply["detail"]["foreign"] == 0


def test_the_threshold_comes_from_the_event_args():
    code, reply = check.run({"args": {"td_check": {"threshold_seconds": 10 ** 9}}}, ".", connect_fn=connection,
                            owner_of=owner_of)
    assert code == 0 and reply["detail"]["threshold_seconds"] == 10 ** 9


def test_an_unreachable_td_is_retryable_and_a_dry_run_touches_nothing():
    def down():
        raise ConnectionError("td is down")
    code, reply = check.run({"args": {}}, ".", connect_fn=down)
    assert (code, reply["error_code"], reply["retryable"]) == (75, "TD_UNAVAILABLE", True)
    assert check_reply(code, reply) == []
    code, reply = check.run({"args": {}}, ".", dry_run=True, connect_fn=lambda: pytest.fail("dry run must not connect"))
    assert (code, reply["status"]) == (0, "not_applicable") and check_reply(code, reply) == []
