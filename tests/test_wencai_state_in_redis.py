# DATA: spec (state keys, TTLs and failure behaviour of the throttle state; no market data involved)
"""Wencai throttle/budget state lives in Redis under core:provider:*; an unreachable Redis fails closed."""
from datetime import datetime

import pytest

from engine_core.data_pipeline.providers.limit_up_today_sources import WencaiThrottled, authorize_wencai_request
from engine_core.runtime_state import ProviderState, StateStore, StateUnavailable
from tests.memory_redis import MemoryRedis

NOW = datetime.fromisoformat("2026-10-28T15:20:01+08:00")


def test_the_daily_budget_is_one_request_claimed_atomically_in_redis():
    redis = MemoryRedis()
    state = StateStore(redis)
    first = authorize_wencai_request(state, "2026-10-28", NOW)
    assert first["request_count"] == 1 and first["last_request_at"].startswith("2026-10-28T15:20:01")
    with pytest.raises(WencaiThrottled, match="wencai_daily_request_limit_reached"):
        authorize_wencai_request(state, "2026-10-28", NOW)
    assert authorize_wencai_request(state, "2026-10-29", NOW)["request_count"] == 1      # another day, another budget


def test_an_unreachable_redis_blocks_the_request_instead_of_removing_the_throttle():
    with pytest.raises(StateUnavailable):
        authorize_wencai_request(StateStore(MemoryRedis(down=True)), "2026-10-28", NOW)


def test_provider_state_round_trips_and_keys_must_stay_under_core():
    redis = MemoryRedis()
    provider = ProviderState("wencai_ladder", StateStore(redis))
    assert provider.load() == {}
    provider.save({"last_request": 1793690187.5, "requests": [1793690167.5]})
    assert provider.load()["last_request"] == 1793690187.5
    assert list(redis.values) == ["core:provider:wencai_ladder:state"]
    with pytest.raises(ValueError, match="core:"):
        StateStore(redis).get("cache:q2:600519")


def test_provider_state_goes_to_redis_only_when_a_redis_is_configured(monkeypatch):
    from engine_core.runtime_state import redis_configured
    monkeypatch.delenv("CORE_REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("CORE_PROVIDER_STATE", raising=False)
    assert redis_configured() is False
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6380/0")
    assert redis_configured() is True
    monkeypatch.setenv("CORE_PROVIDER_STATE", "memory")                 # explicit in-process mode wins (offline runs)
    assert redis_configured() is False
