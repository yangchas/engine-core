"""Daily-kline fill: per-symbol atomic TD writes, resume from TD, fail-fast, Redis lease (real Baostock fixture)."""
import json
from pathlib import Path

import pytest

from engine_core.daily_kline_capture import parse_row
from engine_core.data_pipeline.daily_kline_fill import run_daily_kline_fill
from engine_core.data_pipeline.lease import RedisLease
from engine_core.data_pipeline.td_sink import BAOSTOCK_PROGRESS_VERSION, MemoryTdStore

RAW = json.loads((Path(__file__).parent / "fixtures/baostock_range/range_20261026_20261030.json").read_text())
DAYS = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]
TARGET = DAYS[-1]
SYMBOLS = {"000001", "600519", "688981"}


class FixtureClient:
    """Serves the recorded Baostock rows through the same parser the real range client uses."""

    def __init__(self, fail=None, empty=False):
        self.queries, self.fail, self.empty = [], fail, empty
        self.poisoned = False
        self.logged_in = self.logged_out = False

    def login(self):
        self.logged_in = True

    def logout(self):
        self.logged_out = True

    def query(self, symbol, start, end):
        self.queries.append((symbol, start, end))
        if self.fail:
            raise self.fail
        if self.empty:
            return []
        rows = []
        for item in next(v for k, v in RAW.items() if k.endswith(symbol)):
            row = parse_row(item)
            row["pre_close"] = parse_row({**item, "pre_close": item.get("preclose")})["pre_close"]
            if start <= row["date"] <= end:
                rows.append(row)
        return rows


class FakeRedis:
    def __init__(self):
        self.data = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    def eval(self, script, numkeys, key, owner, *rest):
        if self.data.get(key) != owner:
            return 0
        if "del" in script:
            del self.data[key]
        return 1


def fill(store, client, universe=SYMBOLS, **kwargs):
    return run_daily_kline_fill(TARGET, store=store, client=client, trading_dates=DAYS,
                                expected_symbols=lambda _day: set(universe), depth=5,
                                coverage_threshold=kwargs.pop("coverage_threshold", 1.0), **kwargs)


def test_fills_every_missing_symbol_with_one_request_and_one_atomic_write_each():
    store, client = MemoryTdStore(), FixtureClient()
    result = fill(store, client)
    assert result["status"] == "COMPLETE" and result["written_symbols"] == 3
    assert sorted(symbol for symbol, _s, _e in client.queries) == sorted(SYMBOLS)
    assert all(start == DAYS[0] and end == TARGET for _s, start, end in client.queries)
    assert store.calls.count("upsert_symbol") == 3  # all five days of a symbol arrive in one statement
    for day in DAYS:
        assert set(store.days[day]) == SYMBOLS
        assert {row["version"] for row in store.days[day].values()} == {BAOSTOCK_PROGRESS_VERSION}
    assert client.logged_in and client.logged_out


def test_resumes_from_td_and_only_requests_what_is_missing():
    store, first = MemoryTdStore(), FixtureClient()
    fill(store, first, universe={"000001"})
    client = FixtureClient()
    result = fill(store, client)
    assert result["status"] == "COMPLETE"
    assert sorted(symbol for symbol, _s, _e in client.queries) == ["600519", "688981"]


def test_partial_coverage_at_threshold_stops_early():
    store, client = MemoryTdStore(), FixtureClient()
    result = fill(store, client, coverage_threshold=0.6)
    assert result["status"] == "COMPLETE"
    assert len(client.queries) == 2 and result["coverage"][TARGET] == pytest.approx(0.6667, abs=1e-4)


def test_complete_data_means_no_fetch_login_or_marker_change():
    store = MemoryTdStore()
    fill(store, FixtureClient())
    store.versions[TARGET] = [{"ts": 1, "version": "v1", "provider": "baostock"}]
    client = FixtureClient()
    assert fill(store, client)["status"] == "NO_GAPS"
    assert client.queries == [] and not client.logged_in
    assert store.versions[TARGET]  # an untouched complete day keeps its completion marker


def test_consecutive_failures_abort_early_instead_of_walking_the_universe():
    universe = {f"{n:06d}" for n in range(100, 140)}
    client = FixtureClient(fail=RuntimeError("baostock_query_failed"))
    with pytest.raises(RuntimeError, match=r"BAOSTOCK_GAP_UNRESOLVED:consecutive_failures"):
        fill(MemoryTdStore(), client, universe=universe, max_consecutive_failures=3)
    assert len(client.queries) == 3 and client.logged_out


def test_failure_ratio_aborts_after_enough_samples():
    universe = {f"{n:06d}" for n in range(100, 160)}

    class Flaky(FixtureClient):
        def query(self, symbol, start, end):
            self.queries.append((symbol, start, end))
            if len(self.queries) % 2:
                raise RuntimeError("baostock_query_failed")
            return []

    client = Flaky()
    with pytest.raises(RuntimeError, match="failure_ratio"):
        fill(MemoryTdStore(), client, universe=universe, max_consecutive_failures=99,
             min_failure_samples=10, max_failure_ratio=0.3, max_consecutive_empty=99)
    assert len(client.queries) < 20


def test_source_not_delivered_stops_after_a_run_of_empty_answers():
    universe = {f"{n:06d}" for n in range(100, 160)}
    client = FixtureClient(empty=True)
    with pytest.raises(RuntimeError, match="source_not_delivered"):
        fill(MemoryTdStore(), client, universe=universe, max_consecutive_empty=5)
    assert len(client.queries) == 5


def test_poisoned_session_aborts_at_once():
    class Poisoned(FixtureClient):
        def query(self, symbol, start, end):
            self.queries.append((symbol, start, end))
            self.poisoned = True
            raise TimeoutError("BAOSTOCK_TIMEOUT")

    client = Poisoned()
    with pytest.raises(RuntimeError, match="session_poisoned"):
        fill(MemoryTdStore(), client, universe={f"{n:06d}" for n in range(100, 120)})
    assert len(client.queries) == 1


def test_below_threshold_at_the_end_is_a_retryable_failure_with_progress_kept():
    store = MemoryTdStore()
    with pytest.raises(RuntimeError, match="coverage_below_threshold"):
        fill(store, FixtureClient(), universe=SYMBOLS | {"000002"})
    assert set(store.days[TARGET]) == SYMBOLS  # what was fetched stays in TD for the next trigger


def test_lease_makes_a_second_trigger_skip_without_touching_baostock():
    redis = FakeRedis()
    holder = RedisLease(redis, "lease:daily_kline_fill", owner="other")
    assert holder.acquire()
    client = FixtureClient()
    result = fill(MemoryTdStore(), client, lease=RedisLease(redis, "lease:daily_kline_fill", owner="me"))
    assert result["status"] == "BUSY" and client.queries == [] and not client.logged_in
    assert redis.data["lease:daily_kline_fill"] == "other"  # the loser never releases the holder's lease


def test_lease_is_released_after_the_run_and_lost_lease_stops_the_loop():
    redis = FakeRedis()
    lease = RedisLease(redis, "k", owner="me")
    fill(MemoryTdStore(), FixtureClient(), lease=lease)
    assert "k" not in redis.data

    class Stolen(RedisLease):
        def renew(self):
            redis.data["k"] = "thief"
            return super().renew()

    client = FixtureClient()
    with pytest.raises(RuntimeError, match="lease_lost"):
        fill(MemoryTdStore(), client, lease=Stolen(redis, "k", owner="me"))
    assert client.queries == []


def test_legacy_chain_labelled_day_is_refetched_from_baostock():
    store = MemoryTdStore()
    store.ensure_schema()
    store.days[TARGET] = {"000001": {"close": 1.0, "version": "old"}}
    store.versions[TARGET] = [{"ts": 1, "version": "old", "provider": "baostock>wencai>q2_final"}]
    result = fill(store, FixtureClient(), universe={"000001"})
    assert result["status"] == "COMPLETE"
    assert store.days[TARGET]["000001"]["close"] != 1.0 and store.versions.get(TARGET) is None
