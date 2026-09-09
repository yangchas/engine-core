from datetime import datetime

import pytest

from examples.run_engine_next_context_probe import (
    GuardRedis,
    _parse_now,
    _strict_symbols,
)


def test_context_probe_normalizes_and_sorts_symbols():
    assert _strict_symbols("600519,000001,600519") == ("000001", "600519")


def test_context_probe_rejects_invalid_symbols():
    with pytest.raises(ValueError):
        _strict_symbols("600519,ABC")


def test_context_probe_attaches_explicit_shanghai_timezone_to_local_time():
    parsed = _parse_now("2026-09-10T09:26:00", timezone_name="Asia/Shanghai")
    assert parsed.tzinfo is not None
    assert parsed.isoformat() == "2026-09-10T09:26:00+08:00"


def test_context_probe_preserves_aware_timestamp():
    parsed = _parse_now("2026-09-10T01:26:00+00:00", timezone_name="Asia/Shanghai")
    assert parsed == datetime.fromisoformat("2026-09-10T01:26:00+00:00")


def test_guard_redis_blocks_mutation_without_touching_reads():
    class FakeRedis:
        def get(self, key):
            return "value:" + key

        def set(self, *args, **kwargs):  # pragma: no cover - must be blocked
            raise AssertionError("underlying write must not run")

    guarded = GuardRedis(FakeRedis())
    assert guarded.get("k") == "value:k"
    with pytest.raises(RuntimeError):
        guarded.set("k", "v")
    assert guarded.writes == ["set"]


def test_guard_redis_blocks_pipeline_mutation():
    class FakePipeline:
        def get(self, key):
            return self

        def execute(self):
            return ["value"]

        def set(self, *args, **kwargs):  # pragma: no cover - must be blocked
            raise AssertionError("underlying pipeline write must not run")

    class FakeRedis:
        def pipeline(self, *args, **kwargs):
            return FakePipeline()

    guarded = GuardRedis(FakeRedis())
    pipeline = guarded.pipeline()
    assert pipeline.get("k").execute() == ["value"]
    with pytest.raises(RuntimeError):
        pipeline.set("k", "v")
    assert guarded.writes == ["pipeline.set"]
