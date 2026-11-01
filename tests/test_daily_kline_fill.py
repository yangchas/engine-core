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
    """Only what the guarded production Redis allows: SET NX EX, GET, EXPIRE, DEL (no EVAL)."""

    def __init__(self):
        self.data = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    def get(self, key):
        return self.data.get(key)

    def expire(self, key, seconds):
        return 1 if key in self.data else 0

    def delete(self, key):
        return 1 if self.data.pop(key, None) is not None else 0


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
    fields = ("open", "high", "low", "close", "pre_close", "pct_chg", "amount_yuan", "volume")
    for day in DAYS:
        store.versions[day] = [{"ts": 1, "version": "v1", "provider": "baostock", "row_count": 3,
                                "source_date_values": [day], "source_dates_consistent": True,
                                "source_dates": {f: day for f in fields}}]
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
    holder = RedisLease(redis, "core:lease:daily_kline_fill", owner="other")
    assert holder.acquire()
    client = FixtureClient()
    result = fill(MemoryTdStore(), client, lease=RedisLease(redis, "core:lease:daily_kline_fill", owner="me"))
    assert result["status"] == "BUSY" and client.queries == [] and not client.logged_in
    assert redis.data["core:lease:daily_kline_fill"] == "other"  # the loser never releases the holder's lease


def test_lease_is_released_after_the_run_and_lost_lease_stops_the_loop():
    redis = FakeRedis()
    lease = RedisLease(redis, "core:lease:k", owner="me")
    fill(MemoryTdStore(), FixtureClient(), lease=lease)
    assert "core:lease:k" not in redis.data

    class Stolen(RedisLease):
        def renew(self):
            redis.data["core:lease:k"] = "thief"
            return super().renew()

    client = FixtureClient()
    with pytest.raises(RuntimeError, match="lease_lost"):
        fill(MemoryTdStore(), client, lease=Stolen(redis, "core:lease:k", owner="me"))
    assert client.queries == []


def test_legacy_chain_labelled_day_is_refetched_from_baostock():
    store = MemoryTdStore()
    store.ensure_schema()
    store.days[TARGET] = {"000001": {"close": 1.0, "version": "old"}}
    store.versions[TARGET] = [{"ts": 1, "version": "old", "provider": "baostock>wencai>q2_final"}]
    result = fill(store, FixtureClient(), universe={"000001"})
    assert result["status"] == "COMPLETE"
    assert store.days[TARGET]["000001"]["close"] != 1.0 and store.versions.get(TARGET) is None


def test_completion_marker_is_published_on_coverage_alone(tmp_path):
    from engine_core.data_pipeline.daily_kline_fill import publish_filled_days
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from tests.test_daily_kline_gapfill import _seed_real_symbol_universe

    store = MemoryTdStore()
    for day in DAYS:  # production has one symbol_universe partition per trade day
        _seed_real_symbol_universe(tmp_path, day, sorted(SYMBOLS))
    done = {}
    result = run_daily_kline_fill(
        TARGET, store=store, client=FixtureClient(), trading_dates=DAYS, expected_symbols=lambda _d: set(SYMBOLS),
        depth=5, coverage_threshold=0.995, on_complete=lambda r: done.update(
            partitions=publish_filled_days(tmp_path, store, r["work_days"], lambda _d: set(SYMBOLS), 0.995)))
    assert result["status"] == "COMPLETE"
    assert {item["business_status"] for item in done["partitions"]} == {"FORMAL"}
    assert {item["td_publish"] for item in done["partitions"]} == {"READY"}
    version = store.versions[TARGET][-1]
    assert version["provider"] == "baostock" and version["row_count"] == 3
    manifest = json.loads(ArtifactCatalog(tmp_path).locate("daily_kline", TARGET).read_text())
    assert manifest["provider"] == "baostock" and manifest["business_status"] == "FORMAL"
    # a rerun sees complete days and leaves the marker alone
    again = run_daily_kline_fill(
        TARGET, store=store, client=FixtureClient(), trading_dates=DAYS, expected_symbols=lambda _d: set(SYMBOLS),
        depth=5, coverage_threshold=0.995)
    assert again["status"] == "NO_GAPS" and store.versions[TARGET]


def test_publish_rejects_a_threshold_the_formal_classifier_would_override(tmp_path):
    from engine_core.data_pipeline.daily_kline_fill import publish_filled_days

    with pytest.raises(ValueError, match="coverage_threshold_below_formal_floor"):
        publish_filled_days(tmp_path, MemoryTdStore(), [TARGET], lambda _d: set(SYMBOLS), 0.9)


def test_lease_keys_must_live_under_the_core_prefix_the_redis_guard_allows():
    with pytest.raises(ValueError, match="lease_key_must_start_with"):
        RedisLease(FakeRedis(), "lease:daily_kline_fill")


def test_redis_failure_on_the_lease_is_retryable_not_final():
    class Down(RedisLease):
        def acquire(self):
            raise ConnectionError("redis down")

    client = FixtureClient()
    with pytest.raises(RuntimeError, match=r"BAOSTOCK_GAP_UNRESOLVED:lease_unavailable"):
        fill(MemoryTdStore(), client, lease=Down(FakeRedis(), "core:lease:x"))
    assert client.queries == []


def test_rerun_publishes_the_completion_marker_a_killed_run_never_wrote(tmp_path):
    # Rows reached the threshold but the process died before the marker: the rerun used to say NO_GAPS forever,
    # leaving postmarket waiting for a FORMAL Baostock partition that nothing would ever publish.
    from engine_core.data_pipeline.daily_kline_fill import publish_filled_days
    from tests.test_daily_kline_gapfill import _seed_real_symbol_universe

    for day in DAYS:
        _seed_real_symbol_universe(tmp_path, day, sorted(SYMBOLS))
    store = MemoryTdStore()
    fill(store, FixtureClient())  # no on_complete: the run "died" before publishing
    assert not store.versions.get(TARGET)

    published = {}
    result = run_daily_kline_fill(
        TARGET, store=store, client=FixtureClient(), trading_dates=DAYS, expected_symbols=lambda _d: set(SYMBOLS),
        depth=5, coverage_threshold=0.995, on_complete=lambda r: published.update(
            days=r["publish_days"], parts=publish_filled_days(tmp_path, store, r["publish_days"],
                                                              lambda _d: set(SYMBOLS), 0.995)))
    assert result["status"] == "COMPLETE" and result["written_symbols"] == 0
    assert published["days"] == DAYS and {p["business_status"] for p in published["parts"]} == {"FORMAL"}
    assert store.versions[TARGET][-1]["provider"] == "baostock"

    again = run_daily_kline_fill(
        TARGET, store=store, client=FixtureClient(), trading_dates=DAYS, expected_symbols=lambda _d: set(SYMBOLS),
        depth=5, coverage_threshold=0.995, on_complete=lambda r: pytest.fail("a published day must not republish"))
    assert again["status"] == "NO_GAPS"


def test_a_missing_catalog_manifest_also_counts_as_unpublished():
    store = MemoryTdStore()
    fill(store, FixtureClient())
    fields = ("open", "high", "low", "close", "pre_close", "pct_chg", "amount_yuan", "volume")
    for day in DAYS:  # every day carries a consistent TD marker
        store.versions[day] = [{"ts": 1, "version": "v", "provider": "baostock", "row_count": 3,
                                "source_date_values": [day], "source_dates_consistent": True,
                                "source_dates": {f: day for f in fields}}]
    quiet = run_daily_kline_fill(
        TARGET, store=store, client=FixtureClient(), trading_dates=DAYS, expected_symbols=lambda _d: set(SYMBOLS),
        depth=5, coverage_threshold=0.995, needs_publish=lambda day: False)
    assert quiet["status"] == "NO_GAPS"
    seen = []
    result = run_daily_kline_fill(
        TARGET, store=store, client=FixtureClient(), trading_dates=DAYS, expected_symbols=lambda _d: set(SYMBOLS),
        depth=5, coverage_threshold=0.995, needs_publish=lambda day: day == TARGET,
        on_complete=lambda r: seen.append(r["publish_days"]))
    assert result["status"] == "COMPLETE" and seen == [[TARGET]]


def test_a_stale_marker_row_count_is_republished():
    store = MemoryTdStore()
    fill(store, FixtureClient())
    fields = ("open", "high", "low", "close", "pre_close", "pct_chg", "amount_yuan", "volume")
    for day in DAYS:
        store.versions[day] = [{"ts": 1, "version": "v", "provider": "baostock", "row_count": 2,
                                "source_date_values": [day], "source_dates_consistent": True,
                                "source_dates": {f: day for f in fields}}]
    seen = []
    run_daily_kline_fill(
        TARGET, store=store, client=FixtureClient(), trading_dates=DAYS, expected_symbols=lambda _d: set(SYMBOLS),
        depth=5, coverage_threshold=0.995, on_complete=lambda r: seen.append(r["publish_days"]))
    assert seen == [DAYS]


@pytest.mark.parametrize("manifest,expected", [
    ({"provider": "baostock", "business_status": "FORMAL", "quality": {"coverage": 1.0}}, True),
    ({"provider": "baostock", "business_status": "PROVISIONAL", "quality": {"coverage": 0.9954}}, True),
    ({"provider": "baostock", "business_status": "PROVISIONAL", "quality": {"coverage": 0.98}}, False),
    ({"provider": "baostock", "business_status": "PARTIAL", "quality": {"coverage": 0.999}}, False),
    ({"provider": "wencai", "business_status": "PROVISIONAL", "quality": {"coverage": 1.0}}, False),
    (None, False),
], ids=["formal", "provisional-enough-coverage", "provisional-low-coverage", "partial", "wencai", "absent"])
def test_published_means_baostock_with_enough_coverage_whatever_the_formal_downgrade(tmp_path, manifest, expected):
    from types import SimpleNamespace
    from engine_core.data_pipeline.daily_kline_fill import _catalog_has_published_baostock

    path = tmp_path / "m.json"
    if manifest is not None:
        path.write_text(json.dumps(manifest))
    catalog = SimpleNamespace(locate=lambda _n, _d: path if manifest is not None else None)
    assert _catalog_has_published_baostock(catalog, TARGET, 0.995) is expected


def test_a_provisional_day_with_enough_coverage_is_not_published_again_on_every_trigger(tmp_path):
    # Real shape from the 2026-10-26 rehearsal: coverage ok, FORMAL impossible (a row with empty volume).
    from engine_core.data_pipeline.daily_kline_fill import _catalog_has_published_baostock, publish_filled_days
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from tests.test_daily_kline_gapfill import _seed_real_symbol_universe

    class GappyVolume(FixtureClient):
        def query(self, symbol, start, end):
            rows = super().query(symbol, start, end)
            return [{**row, "volume": None} if symbol == "000001" else row for row in rows]

    for day in DAYS:
        _seed_real_symbol_universe(tmp_path, day, sorted(SYMBOLS))
    store, catalog = MemoryTdStore(), ArtifactCatalog(tmp_path)
    publishes = []

    def run(client):
        return run_daily_kline_fill(
            TARGET, store=store, client=client, trading_dates=DAYS, expected_symbols=lambda _d: set(SYMBOLS),
            depth=5, coverage_threshold=0.995,
            needs_publish=lambda day: not _catalog_has_published_baostock(catalog, day, 0.995),
            on_complete=lambda r: publishes.append(publish_filled_days(
                tmp_path, store, r["publish_days"], lambda _d: set(SYMBOLS), 0.995)))

    assert run(GappyVolume())["status"] == "COMPLETE"
    assert {p["business_status"] for p in publishes[0]} == {"PROVISIONAL"}
    assert run(GappyVolume())["status"] == "NO_GAPS" and len(publishes) == 1
