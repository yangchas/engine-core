from datetime import datetime, timezone

import pytest

from engine_core.q2_tracker import (Q2Tracker, count_q2_hashes, read_q2_hashes,
                                    publish_q2_snapshot)


def _row(px, pc="10000", ts="1792062000000", **extra):
    return {"px": str(px), "pc": str(pc), "amt": "1000", "spd1m": "25",
            "ts": ts, "ls": "0", **{k: str(v) for k, v in extra.items()}}


def test_q2_read_and_freshness_marks_stale_without_zero_filling():
    now = datetime.fromtimestamp(1792062000, timezone.utc)

    class Pipe:
        def __init__(self, client): self.client, self.keys = client, []
        def hgetall(self, key): self.keys.append(key)
        def execute(self): return [self.client.rows.get(key, {}) for key in self.keys]
    class Redis:
        rows = {"q2:000001": _row(10100), "q2:000002": _row(9900, ts="1792061879000"),
                "q2:000003": {"px": "0", "pc": "10000", "ts": "1792062000000"}}
        def scan_iter(self, match, count): return iter(self.rows)
        def pipeline(self): return Pipe(self)

    rows = read_q2_hashes(Redis())
    result = Q2Tracker({}, {}).observe(rows, now)
    assert result["market"]["symbol_count"] == 3
    assert result["market"]["stale_count"] == 1
    assert result["stocks"]["000002"]["status"] == "STALE"
    assert result["stocks"]["000003"]["pct_change"] is None
    assert result["market"]["amount_yuan"] == 1000


def test_q2_coverage_counts_hash_keys_in_batched_type_pipeline():
    class Pipe:
        def __init__(self, redis): self.redis, self.keys = redis, []
        def type(self, key): self.keys.append(key)
        def execute(self): return [self.redis.types.get(key, b"none") for key in self.keys]

    class Redis:
        keys = ["q2:000001", "q2:000002", "q2:000004", "q2:bad", "other:000003"]
        types = {"q2:000001": b"hash", "q2:000002": "hash"}
        def scan_iter(self, match, count): return iter(self.keys)
        def pipeline(self): return Pipe(self)

    assert count_q2_hashes(Redis()) == 3


def test_plate_aggregation_uses_equal_weight_and_injected_baselines():
    now = datetime.fromtimestamp(1792062000, timezone.utc)
    rows = {"000001": _row(10100, amt="100", spd1m="100"),
            "000002": _row(9900, amt="300", spd1m="-100")}
    tracker = Q2Tracker({"801001": ["000001", "000002"]},
                        {"000001": {"p25": 1, "p45": 2}, "000002": {"p25": 3, "p45": 4}},
                        plate_names={"801001": "示例板块"})
    out = tracker.observe(rows, now)["plates"][0]
    assert out["equal_weight_pct"] == 0
    assert out["amount_yuan"] == 400
    assert out["spd1m_bp_mean"] == 0
    assert out["vs_0925_pp"] == -2
    assert out["vs_0945_pp"] == -3


def test_high_stock_sealed_opened_resealed_records_transition_times():
    start = datetime.fromtimestamp(1792062000, timezone.utc)
    tracker = Q2Tracker({}, {}, high_limit_pool={"000001": 2})
    first = tracker.observe({"000001": _row(11000, ls="1")}, start)["high_stocks"]["000001"]
    second = tracker.observe({"000001": _row(10800, ls="0")}, start.replace(minute=1))["high_stocks"]["000001"]
    third = tracker.observe({"000001": _row(11000, ls="1")}, start.replace(minute=2))["high_stocks"]["000001"]
    assert [first["state"], second["state"], third["state"]] == ["封死", "开板", "回封"]
    assert [second["transition_at"], third["transition_at"]] == [
        start.replace(minute=1).isoformat(), start.replace(minute=2).isoformat()]


def test_publish_checks_full_isolation_namespace_and_positive_ttl():
    class Redis:
        def __init__(self): self.calls = []
        def set(self, key, value, ex=None): self.calls.append((key, value, ex))
    redis = Redis()
    publish_q2_snapshot(redis, "2026-10-15", {"ok": True}, 3600, suffix="latest")
    assert redis.calls[0][0] == "core:cache:live_tracking:2026-10-15:q2:latest"
    assert redis.calls[0][2] == 3600
    for key, ttl in (("core:cache:live_tracking:2026-10-16:q2:latest", 60),
                     ("cache:other", 60), (None, 0)):
        with pytest.raises(ValueError):
            publish_q2_snapshot(redis, "2026-10-15", {}, ttl, key=key)
