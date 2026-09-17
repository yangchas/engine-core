from datetime import datetime

import pytest

from examples.run_engine_next_context_probe import (
    GuardRedis,
    _parse_now,
    _phase_for_request,
    _opening_behavior_rows,
    _strict_symbols,
)
from engine_core import normalize_auction_change_ratio


def test_context_probe_normalizes_and_sorts_symbols():
    assert _strict_symbols("600519,000001,600519") == ("000001", "600519")


def test_context_probe_rejects_invalid_symbols():
    with pytest.raises(ValueError):
        _strict_symbols("600519,ABC")


def test_opening_behavior_audit_is_bounded_and_uses_full_context_floor():
    from types import SimpleNamespace

    snapshots = (
        SimpleNamespace(
            symbol="600519",
            open_pct=0.0,
            current_pct=0.04,
            auction_amount=10_000_000.0,
            amount_2m=30_000_000.0,
            speed_1m=0.01,
        ),
        SimpleNamespace(
            symbol="000001",
            open_pct=0.02,
            current_pct=0.03,
            auction_amount=5_000_000.0,
            amount_2m=5_000_000.0,
            speed_1m=0.0,
        ),
    )

    def floor(rows, attr_name, *, top_n, fallback):
        assert rows == snapshots
        assert attr_name == "amount_2m"
        assert top_n == 160
        assert fallback == 20_000_000
        return 7_500_000.0

    def classify(row, *, amount_2m_floor):
        return f"{row.symbol}:{amount_2m_floor:.0f}"

    result = _opening_behavior_rows(
        {
            "relative_amount_floor": floor,
            "classify_opening_entry_behavior": classify,
        },
        snapshots,
        ("000001", "600519", "300001"),
    )

    assert result[0]["behavior"] == "000001:7500000"
    assert result[1]["behavior"] == "600519:7500000"
    assert result[2] == {
        "symbol": "300001",
        "status": "MISSING",
        "amount_2m_floor_yuan": 7_500_000.0,
        "behavior": None,
    }


def test_context_probe_attaches_explicit_shanghai_timezone_to_local_time():
    parsed = _parse_now("2026-09-10T09:26:00", timezone_name="Asia/Shanghai")
    assert parsed.tzinfo is not None
    assert parsed.isoformat() == "2026-09-10T09:26:00+08:00"


def test_context_probe_preserves_aware_timestamp():
    parsed = _parse_now("2026-09-10T01:26:00+00:00", timezone_name="Asia/Shanghai")
    assert parsed == datetime.fromisoformat("2026-09-10T01:26:00+00:00")


def test_context_probe_delegates_phase_to_legacy_authority():
    expected = object()
    legacy = {"infer_run_phase": lambda now: expected}
    assert _phase_for_request(
        legacy,
        _parse_now("2026-09-10T09:35:00", timezone_name="Asia/Shanghai"),
    ) is expected


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


def test_guard_redis_allows_hash_length_read_used_by_intraday_context():
    class FakeRedis:
        def hlen(self, key):
            return 3

    guarded = GuardRedis(FakeRedis())
    assert guarded.hlen("hot-rank") == 3


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


def test_guard_redis_records_only_selected_direct_hash_reads():
    class FakeRedis:
        def hgetall(self, key):
            return {"ts": "123", "px": key}

    guarded = GuardRedis(FakeRedis(), record_keys=frozenset({"q2:600519"}))
    assert guarded.hgetall("q2:000001")["px"] == "q2:000001"
    assert guarded.hgetall("q2:600519")["px"] == "q2:600519"
    assert guarded.reads == [
        {
            "operation": "hgetall",
            "key": "q2:600519",
            "value": {"ts": "123", "px": "q2:600519"},
        }
    ]


def test_guard_redis_records_selected_pipeline_hash_read_without_changing_order():
    class FakePipeline:
        def __init__(self):
            self.commands = []

        def get(self, key):
            self.commands.append(("get", key))
            return self

        def hgetall(self, key):
            self.commands.append(("hgetall", key))
            return self

        def execute(self):
            return [
                "plain-value" if command == "get" else {"ts": "456", "px": key}
                for command, key in self.commands
            ]

    class FakeRedis:
        def pipeline(self, *args, **kwargs):
            return FakePipeline()

    guarded = GuardRedis(FakeRedis(), record_keys=frozenset({"q2:600519"}))
    results = (
        guarded.pipeline()
        .get("plain")
        .hgetall("q2:000001")
        .hgetall("q2:600519")
        .execute()
    )

    assert results == [
        "plain-value",
        {"ts": "456", "px": "q2:000001"},
        {"ts": "456", "px": "q2:600519"},
    ]
    assert guarded.reads == [
        {
            "operation": "hgetall",
            "key": "q2:600519",
            "value": {"ts": "456", "px": "q2:600519"},
        }
    ]


def test_guard_redis_blocks_chained_pipeline_mutation_and_low_level_command():
    class FakePipeline:
        def get(self, key):
            return self

        def execute(self):
            return ["value"]

        def execute_command(self, command, *args, **kwargs):
            return self

        def set(self, *args, **kwargs):  # pragma: no cover - must be blocked
            raise AssertionError("underlying pipeline write must not run")

    class FakeRedis:
        def pipeline(self, *args, **kwargs):
            return FakePipeline()

        def execute_command(self, command, *args, **kwargs):
            return "read"

    guarded = GuardRedis(FakeRedis())
    with pytest.raises(RuntimeError):
        guarded.pipeline().get("k").set("k", "v")
    with pytest.raises(RuntimeError):
        guarded.execute_command("SET", "k", "v")
    assert guarded.writes == ["pipeline.set", "command.set"]


def test_guard_redis_rejects_unclassified_client_method():
    class FakeRedis:
        def custom_mutation(self):
            return None

    guarded = GuardRedis(FakeRedis())
    with pytest.raises(RuntimeError, match="unclassified Redis method"):
        guarded.custom_mutation()


def test_guard_redis_rejects_unclassified_pipeline_method():
    class FakePipeline:
        def custom_mutation(self):  # pragma: no cover - must be blocked
            raise AssertionError("underlying pipeline method must not run")

    class FakeRedis:
        def pipeline(self):
            return FakePipeline()

    guarded = GuardRedis(FakeRedis())
    with pytest.raises(RuntimeError, match="unclassified Redis pipeline method"):
        guarded.pipeline().custom_mutation()


@pytest.mark.parametrize(
    ("raw", "expected"),
    ((0.0997, 0.0997), (9.97, 0.0997), (997, 0.0997), (-9.97, -0.0997)),
)
def test_auction_ratio_wheel_preserves_legacy_formula_for_valid_values(raw, expected):
    assert normalize_auction_change_ratio(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", (None, "", "bad", True, float("nan"), float("inf")))
def test_auction_ratio_wheel_keeps_missing_or_invalid_as_unknown(raw):
    assert normalize_auction_change_ratio(raw) is None
