# DATA: real:td/show_queries.json.gz (SHOW QUERIES recorded 2026-11-04 with the client's real command line); real:proc/meminfo_2026-11-05_1805.txt, proc/taosd_status_2026-11-05_1805.txt (this host's /proc files, PROVENANCE `_proc_recordings`); derived variant: the same answer with no rows
"""The check names whoever holds TD's query memory: a long query of another process is 'degraded', never silent."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from engine_core.guardian import td_resource_check as check
from engine_core.guardian.contract import check_reply
from tests.memory_redis import MemoryRedis
from tests.replay import ReplayTd, load_recording

RECORDING = load_recording("show_queries")
OWNERS = RECORDING["call"]["owners"]
SQL = RECORDING["statements"][0]["sql"]


REAL = Path(__file__).parent / "fixtures" / "real"


@pytest.fixture(autouse=True)
def recorded_host_memory(tmp_path, monkeypatch):
    """The check reads this host's /proc; the tests read the recorded copies instead (taosd is pid 1105 there)."""
    (tmp_path / "proc" / "1105").mkdir(parents=True)
    (tmp_path / "proc" / "1105" / "status").write_bytes((REAL / "proc" / "taosd_status_2026-11-05_1805.txt").read_bytes())
    meminfo = REAL / "proc" / "meminfo_2026-11-05_1805.txt"
    monkeypatch.setattr(check, "read_memory", lambda: _read_recorded(meminfo, tmp_path / "proc"))


def _read_recorded(meminfo, proc):
    return RECORDED_READ(meminfo=meminfo, proc=proc)


RECORDED_READ = check.read_memory


# DATA: real:/proc of the test host
def test_top_processes_from_real_proc():
    processes = RECORDED_READ()["top_processes"]
    assert 0 < len(processes) <= 8
    assert all(set(p) == {"pid", "name", "rss_mb"} for p in processes)
    assert [p["rss_mb"] for p in processes] == sorted((p["rss_mb"] for p in processes), reverse=True)
    assert any(p["pid"] == os.getpid() for p in processes)


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


def test_a_long_query_of_core_itself_is_reported_not_silent():
    """The 2026-11-05 `show databases` of Core's own handler hung for 72 minutes and the check said ok."""
    own = lambda pid: "/usr/bin/python /home/exedev/repos/engine_core-live/examples/guardian_handler.py run"   # noqa: E731
    code, reply = check.run({"args": {}}, ".", connect_fn=connection, owner_of=own)
    assert (code, reply["status"], reply["error_code"]) == (10, "degraded", "TD_CORE_LONG_QUERY")
    assert reply["detail"]["core"] == 1 and reply["detail"]["foreign"] == 0 and check_reply(code, reply) == []


def test_host_memory_is_read_from_the_recorded_proc_files():
    memory = RECORDED_READ(meminfo=REAL / "proc" / "meminfo_2026-11-05_1805.txt", proc=REAL / "proc" / "none")
    assert memory["total_mb"] == 7557556 // 1024 and memory["available_mb"] == 2243756 // 1024
    assert memory["swap_total_mb"] == 0 and memory["committed_mb"] > memory["commit_limit_mb"]      # overcommitted, no swap
    assert memory["reserve_estimate_mb"] == int(memory["total_mb"] * 0.20)                            # documented default
    assert memory["pool_headroom_estimate_mb"] == memory["available_mb"] - memory["reserve_estimate_mb"]
    assert memory["taosd"] == {}                                                                      # no taosd under that proc


def test_taosd_size_is_found_by_process_name(tmp_path):
    memory = RECORDED_READ(meminfo=REAL / "proc" / "meminfo_2026-11-05_1805.txt", proc=tmp_path / "proc")
    assert memory["taosd"]["pid"] == 1105 and 1000 < memory["taosd"]["rss_mb"] < memory["taosd"]["peak_rss_mb"] + 1


def test_low_pool_headroom_is_degraded_even_when_no_query_is_long():
    ok, reply = check.run({"args": {}}, ".", connect_fn=lambda: connection(rows=[]), owner_of=owner_of)
    assert ok == 0 and reply["detail"]["memory"]["pool_headroom_estimate_mb"] > 0
    limit = reply["detail"]["memory"]["pool_headroom_estimate_mb"] + 1
    code, reply = check.run({"args": {"td_check": {"min_headroom_mb": limit}}}, ".",
                            connect_fn=lambda: connection(rows=[]), owner_of=owner_of)
    assert (code, reply["status"], reply["error_code"]) == (10, "degraded", "TD_MEMORY_HEADROOM_LOW")
    assert reply["detail"]["findings"] == ["TD_MEMORY_HEADROOM_LOW"] and check_reply(code, reply) == []


def test_every_running_query_is_in_the_snapshot_not_only_the_long_ones():
    code, reply = check.run({"args": {"td_check": {"threshold_seconds": 10 ** 9}}}, ".", connect_fn=connection,
                            owner_of=owner_of)
    queries = reply["detail"]["queries"]
    assert code == 0 and reply["detail"]["long_queries"] == []
    assert queries["count"] == 1 and queries["foreign"] == 1 and queries["top"][0]["sql"].startswith("select symbol")


def test_each_run_keeps_its_snapshot_for_three_days():
    from datetime import datetime
    from engine_core.runtime_state import StateStore
    redis = MemoryRedis()
    check.run({"args": {}}, ".", connect_fn=lambda: connection(rows=[]), owner_of=owner_of, state=StateStore(redis),
              now=lambda: datetime(2026, 11, 5, 9, 35, 2))
    key = check.HISTORY_KEY_PREFIX + "20261105T093502"
    assert 0 < redis.ttl(key) <= check.HISTORY_TTL_SECONDS
    assert json.loads(redis.get(key))["memory"]["total_mb"] > 0


def test_a_host_without_proc_still_reports_the_queries(monkeypatch):
    def missing():
        raise FileNotFoundError("/proc/meminfo")
    monkeypatch.setattr(check, "read_memory", missing)
    code, reply = check.run({"args": {}}, ".", connect_fn=lambda: connection(rows=[]), owner_of=owner_of)
    assert code == 0 and reply["detail"]["memory"] is None


def test_the_proc_recordings_match_their_provenance():
    index = json.loads((REAL / "PROVENANCE.json").read_text(encoding="utf-8"))["_proc_recordings"]
    files = {p.relative_to(REAL).as_posix() for p in (REAL / "proc").rglob("*") if p.is_file()}
    assert files == set(index)
    for name, meta in index.items():
        body = (REAL / name).read_bytes()
        assert hashlib.sha256(body).hexdigest() == meta["sha256"] and meta["bytes"] == len(body)
        assert meta["kind"] == "raw_proc_file" and meta["source_path"].startswith("/proc/")
