# DATA: spec (state keys, TTLs, breaker semantics; no market data involved)
"""Job breaker/status state of the JobRunner lives in Redis (core:jobs:*) or in the process; never in job_state.json."""
from datetime import datetime, timedelta

import pytest

from engine_core.data_pipeline import jobs
from engine_core.data_pipeline.jobs import JobRunner, JobState, RequestBudget
from engine_core.runtime_state import StateStore, StateUnavailable
from tests.memory_redis import MemoryRedis

NOW = datetime.fromisoformat("2026-11-04T09:00:00+08:00")


def test_an_open_breaker_is_a_redis_key_that_expires_with_the_cooldown():
    redis = MemoryRedis()
    state = JobState(StateStore(redis))
    state.open_breaker("kaipan", NOW, "provider_blocked", hours=6)
    assert state.breaker("kaipan")["category"] == "provider_blocked"
    assert 6 * 3600 <= redis.ttl("core:jobs:breaker:kaipan") <= 6 * 3600 + 3600


def test_the_runner_refuses_while_a_breaker_is_open_and_keeps_no_state_file(tmp_path):
    redis = MemoryRedis()
    runner = JobRunner(artifact_root=tmp_path, budget=RequestBudget(min_interval_seconds=0),
                       state=JobState(StateStore(redis)))
    runner.state.open_breaker("kaipan", NOW, "provider_blocked", hours=6)
    with pytest.raises(RuntimeError, match="circuit_open:provider_cooldown"):
        runner._check_breaker("kaipan", NOW + timedelta(hours=1))
    runner._check_breaker("kaipan", NOW + timedelta(hours=7))             # cooled down
    assert not (tmp_path / "job_state.json").exists()


def test_job_status_is_per_dataset_with_a_ttl():
    redis = MemoryRedis()
    state = JobState(StateStore(redis))
    state.set_status("hot_plates", {"trade_date": "2026-11-03", "status": "FAILED", "reason": "fetch_failed:X"})
    assert state.status("hot_plates")["status"] == "FAILED"
    assert 0 < redis.ttl("core:jobs:status:hot_plates") <= 7 * 86400


def test_an_unreachable_redis_stops_the_job_instead_of_ignoring_its_breaker(tmp_path):
    runner = JobRunner(artifact_root=tmp_path, budget=RequestBudget(min_interval_seconds=0),
                       state=JobState(StateStore(MemoryRedis(down=True))))
    with pytest.raises(StateUnavailable):
        runner._check_breaker("kaipan", NOW)


def test_without_a_configured_redis_the_default_state_is_in_process(monkeypatch):
    monkeypatch.setenv("CORE_PROVIDER_STATE", "memory")
    state = jobs.default_job_state()
    state.set_status("x", {"status": "READY"})
    assert state.status("x") == {"status": "READY"} and jobs.default_job_state() is not state
