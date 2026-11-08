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


def test_listing_snapshot_excludes_delisted_from_fill_plan(tmp_path):
    from engine_core.data_pipeline.daily_kline_fill import listing_detail, make_expected_symbols
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from test_stock_listing import Client, fetch

    listing = {"version": "fixture", "rows": [row for row in fetch(Client())
               if row["symbol"] in {"sz.301139", "sh.688796"}]}
    expected = make_expected_symbols(ArtifactCatalog(tmp_path), listing=listing)
    assert expected("2026-10-27") == {"301139", "688796"}
    assert expected("2026-10-28") == {"688796"}
    assert expected("2026-10-29") == {"688796"}
    assert listing_detail(listing, ["2026-10-27", "2026-10-28", "2026-10-29"])["skipped_not_listed"] == 2
    store, client, days = _c806_setup()
    result = run_daily_kline_fill(days[-1], store=store, client=client, trading_dates=days,
                                 expected_symbols=expected, depth=len(days), require_all=True)
    assert client.queries == ["688796"]
    assert result["incomplete_total"] == 0


def test_listing_snapshot_includes_new_stock_only_from_ipo(tmp_path):
    from engine_core.data_pipeline.daily_kline_fill import make_expected_symbols
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from test_stock_listing import Client, fetch

    listing = {"version": "fixture", "rows": [row for row in fetch(Client())
               if row["symbol"] in {"sz.001381", "sh.688796", "sh.688797"}]}
    expected = make_expected_symbols(ArtifactCatalog(tmp_path), listing=listing)
    assert "001381" not in expected("2026-11-02")
    assert "001381" in expected("2026-11-03")
    store, client, days = _c806_setup()
    query = client.query
    requests = []
    def recorded(symbol, start, end):
        requests.append((symbol, start, end))
        return [] if symbol == "001381" else query(symbol, start, end)
    client.query = recorded
    run_daily_kline_fill(days[-1], store=store, client=client, trading_dates=days,
                         expected_symbols=expected, depth=len(days), require_all=True)
    assert next(start for symbol, start, _ in requests if symbol == "001381") == "2026-11-03"


def test_listing_unavailable_falls_back_in_production_reply(tmp_path, monkeypatch):
    from engine_core.data_pipeline import daily_kline_fill as module
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from test_stock_listing import Client, fetch

    rows = fetch(Client())
    ArtifactCatalog(tmp_path).commit("symbol_universe", "2026-10-27",
        [{"symbol": row["symbol"].split(".")[-1], "trade_status": row["status"]} for row in rows],
        {"trade_date": "2026-10-27"}, source="baostock")
    monkeypatch.setattr(module, "load_listing_snapshot", lambda days: None)
    def run(target, **kwargs):
        assert "301139" not in kwargs["expected_symbols"](target)
        assert "001381" in kwargs["expected_symbols"](target)
        return {"status": "NO_GAPS"}
    monkeypatch.setattr(module, "run_daily_kline_fill", run)
    result = module.run_daily_kline_fill_from_root(tmp_path, "2026-10-28", store=MemoryTdStore(),
        client=object(), trading_dates=["2026-10-28"], args={"fill_lease": "off"})
    assert result["listing_source"] == "fallback" and result["listing_version"] is None


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


def test_a_provisional_day_with_enough_coverage_is_not_published_again_on_every_trigger(tmp_path):
    # Real shape from the 2026-10-26 rehearsal: coverage ok, FORMAL impossible (a row with empty volume).
    from engine_core.data_pipeline.daily_kline_fill import publish_filled_days
    from engine_core.data_pipeline.published_daily_kline import find_published_baostock_manifest
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
            needs_publish=lambda day: find_published_baostock_manifest(catalog, day, 0.995) is None,
            on_complete=lambda r: publishes.append(publish_filled_days(
                tmp_path, store, r["publish_days"], lambda _d: set(SYMBOLS), 0.995)))

    assert run(GappyVolume())["status"] == "COMPLETE"
    assert {p["business_status"] for p in publishes[0]} == {"PROVISIONAL"}
    assert run(GappyVolume())["status"] == "NO_GAPS" and len(publishes) == 1


# ---- C651: expected symbols for days older than every symbol_universe partition ------------------------------------

def _real_stock_static_rows():
    import gzip
    path = (Path(__file__).parent / "fixtures" / "real" / "core_artifacts" / "c651" / "stock_static"
            / "trade_date=2026-10-29" / "source=baostock_stock_basic" / "sha256-d2f57645aafb76b7" / "rows.jsonl.gz")
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_listed_symbols_follow_the_real_listing_dates():
    from engine_core.data_pipeline.daily_kline_fill import listed_symbols
    rows = _real_stock_static_rows()
    newest = max((r for r in rows if r.get("listing_date")), key=lambda r: r["listing_date"])
    oldest = min((r for r in rows if r.get("listing_date")), key=lambda r: r["listing_date"])
    before = listed_symbols(rows, "2026-05-07")
    assert oldest["symbol"] in before and newest["symbol"] not in before
    assert newest["symbol"] in listed_symbols(rows, newest["listing_date"])
    assert len(before) < len(listed_symbols(rows, "2026-10-29")) <= len(rows)


def test_days_older_than_the_universe_use_listing_dates_newer_days_use_the_universe(tmp_path):
    from engine_core.data_pipeline.daily_kline_fill import listed_symbols, make_expected_symbols
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from tests.test_daily_kline_gapfill import _seed_real_symbol_universe
    rows = _real_stock_static_rows()
    catalog = ArtifactCatalog(tmp_path)
    catalog.commit("stock_static", "2026-10-29", rows, {"business_status": "PROVISIONAL"}, source="baostock_stock_basic")
    _seed_real_symbol_universe(tmp_path, "2026-10-15", ["000001", "000002"])
    expected = make_expected_symbols(ArtifactCatalog(tmp_path))
    assert expected("2026-10-16") == {"000001", "000002"}                 # a universe partition exists at or before
    assert expected("2026-05-07") == listed_symbols(rows, "2026-05-07")    # older than every partition
    assert len(expected("2026-05-07")) > 1000


def test_publish_never_fails_with_known_missing_incomplete_for_a_day_without_a_same_day_universe(tmp_path):
    """C687: the 2026-11-05 deep backfill loaded 110 days of rows but could not publish any completion marker.

    Days older than every symbol_universe partition have no same-day universe, so the quality step judged completion
    against an empty expected set while the ledger listed the missing symbols: ``known_missing_incomplete``."""
    import gzip

    from engine_core.data_pipeline.daily_kline_fill import publish_filled_days
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from engine_core.data_pipeline.td_sink import map_rows

    fixtures = Path(__file__).parent / "fixtures" / "real" / "core_artifacts" / "c540"
    day = "2026-10-21"   # real Baostock rows of that day (c540 partial capture) and the real universe of 2026-10-23
    with gzip.open(fixtures / "daily_kline" / day / "partial" / "rows.jsonl.gz", "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    with gzip.open(fixtures / "symbol_universe" / "2026-10-23" / "actual" / "rows.jsonl.gz", "rt",
                   encoding="utf-8") as handle:
        universe = {str(json.loads(line)["symbol"]).rsplit(".", 1)[-1].zfill(6) for line in handle if line.strip()}
    store = MemoryTdStore()
    store.ensure_schema()
    mapped, dropped = map_rows(rows, {"provider": "baostock"})
    assert not dropped and set(mapped) < universe          # rows exist for part of the universe: the rest is missing
    listed = set(mapped) | set(sorted(universe - set(mapped))[:3])   # the day's rows plus three listed symbols with none
    store.upsert_day(day, BAOSTOCK_PROGRESS_VERSION, mapped)
    assert ArtifactCatalog(tmp_path).locate("symbol_universe", day) is None

    partitions = publish_filled_days(tmp_path, store, [day], lambda _d: set(listed), 0.995)
    assert partitions[0]["td_publish"] == "READY" and store.versions[day][-1]["row_count"] == len(mapped)


def test_window_watermarks_default_and_full_fallback_use_real_symbols(tmp_path):
    import gzip
    from engine_core.data_pipeline.td_sink import DailyKlineTdSink, TaosStore

    fixture = Path(__file__).parent / "fixtures" / "real" / "daily_kline_c756" / "td_symbols_by_day.jsonl.gz"
    with gzip.open(fixture, "rt", encoding="utf-8") as handle:
        captured = [json.loads(line) for line in handle]
    by_day = {item["date"]: set(item["symbols"]) for item in captured[-5:]}
    days = sorted(by_day)

    class Store(MemoryTdStore):
        window_watermarks = TaosStore.window_watermarks

        def __init__(self):
            super().__init__()
            self.queried = []
            self.full_calls = 0
            self.allow_full = False

        def symbols_on_day(self, day):
            self.queried.append(day)
            return by_day[day]

        def latest_watermarks(self):
            assert self.allow_full, "unexpected full-table watermark query"
            self.full_calls += 1
            return {}

    store = Store()
    symbol = next(iter(by_day[days[-1]]))
    sink = DailyKlineTdSink(store, tmp_path)
    sink.prepare_progress(days[-1], [symbol])
    assert store.queried == [days[-1]]
    store.queried.clear()
    from tests.test_daily_kline_gapfill import _seed_td_day
    from td_sink_support import load_real
    _, rows = load_real("formal")
    row = next(row for row in rows if row["symbol"] in set.intersection(*by_day.values()))
    for day in days:
        _seed_td_day(store, day, [{**row, "date": day}])
    kwargs = dict(store=store, client=FixtureClient(), trading_dates=days,
                  expected_symbols=lambda day: {row["symbol"]}, depth=len(days))
    assert run_daily_kline_fill(days[-1], **kwargs)["status"] == "NO_GAPS"
    assert len(store.queried) <= len(days)
    store.allow_full = True
    run_daily_kline_fill(days[-1], **kwargs, watermark_mode="full")
    DailyKlineTdSink(store, tmp_path).prepare_progress(days[-1], [symbol], watermark_mode="full")
    assert store.full_calls == 2


def _c806_setup(empty=False):
    from tests.test_daily_kline_gapfill import _seed_td_day
    path = Path(__file__).parent / 'fixtures/daily_kline_c806/history.jsonl'
    raw = {v['symbol']: v for v in map(json.loads, path.read_text().splitlines())}
    days = ['2026-10-30', '2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06']
    def parsed(symbol, day):
        item = raw[symbol]
        row = dict(zip(item['fields'], next(r for r in item['rows'] if r[0] == day)))
        value = parse_row(row)
        value['pre_close'] = parse_row({**row, 'pre_close': row.get('preclose')})['pre_close']
        return value
    store = MemoryTdStore()
    store.ensure_schema()
    for day in days:
        symbols = ['688797', '688796'] if day == days[0] else ['688797']
        _seed_td_day(store, day, [parsed(symbol, day) for symbol in symbols])
    class Client:
        def __init__(self): self.queries = []
        def login(self): pass
        def logout(self): pass
        def query(self, symbol, start, end):
            self.queries.append(symbol)
            return [] if empty else [parsed(symbol, day) for day in days if start <= day <= end]
    return store, Client(), days


def test_c806_replays_lagging_symbol_above_coverage_threshold():
    store, client, days = _c806_setup()
    result = run_daily_kline_fill(days[-1], store=store, client=client, trading_dates=days,
                                  expected_symbols=lambda day: {'688796', '688797'}, depth=len(days),
                                  coverage_threshold=0.5, require_all=True)
    assert client.queries == ['688796']
    assert all('688796' in store.read_day_rows(day) for day in days)
    assert result['incomplete_total'] == 0


def test_c806_unfilled_plan_is_incomplete_above_coverage_threshold():
    store, client, days = _c806_setup(empty=True)
    def fail(*args):
        raise RuntimeError('request failed')
    client.query = fail
    result = run_daily_kline_fill(days[-1], store=store, client=client, trading_dates=days,
                                  expected_symbols=lambda day: {'688796', '688797'}, depth=len(days),
                                  coverage_threshold=0.5, require_all=True)
    assert result['status'] == 'INCOMPLETE'
    assert result['incomplete_total'] == 5


def test_c806_incomplete_result_reaches_cli_and_guardian(monkeypatch, capsys):
    from examples import backfill_td_daily_kline, guardian_handler
    from engine_core.data_pipeline import daily_kline_fill
    from engine_core.guardian import daily_kline_coverage
    from engine_core.errors import CoreError

    def incomplete(*args, **kwargs):
        exc = CoreError('BAOSTOCK_GAP_UNRESOLVED', 'planned_gaps_remain')
        exc.daily_kline_detail = {'status': 'INCOMPLETE', 'incomplete_total': 5,
                                  'incomplete': [{'symbol': '688796', 'date': '2026-11-02'}]}
        raise exc

    monkeypatch.setattr(daily_kline_fill, 'run_daily_kline_fill_from_root', incomplete)
    monkeypatch.setattr(daily_kline_coverage, 'td_settings', lambda **kwargs: {})
    monkeypatch.setattr(daily_kline_coverage, 'make_store', lambda settings: MemoryTdStore())
    monkeypatch.setenv('TD_CONFIG', 'unused')
    assert backfill_td_daily_kline.main(['--date', '2026-11-06']) == 10
    output = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert output['status'] == 'degraded' and output['detail']['incomplete_total'] == 5
    code, payload = guardian_handler._run_daily_kline_fill({}, '.', '2026-11-06', {})
    assert code == 75 and payload['status'] == 'failed'


def test_c830_guardian_preserves_rechecked_gaps(monkeypatch, tmp_path, capsys):
    from examples import guardian_handler
    from engine_core.data_pipeline import daily_kline_fill as module
    from engine_core.guardian.contract import check_reply

    store, client, days = _c806_setup()
    def fail(*args):
        raise RuntimeError('request failed')
    client.query = fail
    monkeypatch.setattr(module, 'load_listing_snapshot', lambda days: None)
    monkeypatch.setattr(module, 'make_expected_symbols', lambda *args, **kwargs: lambda day: {'688796', '688797'})
    run = module.run_daily_kline_fill_from_root
    monkeypatch.setattr(module, 'run_daily_kline_fill_from_root', lambda root, target, args: run(
        root, target, args={'fill_lease': 'off', 'history_min_trading_days': len(days)},
        store=store, client=client, trading_dates=days))
    code, reply = guardian_handler._run_daily_kline_fill({}, tmp_path, days[-1], {})
    assert (code, reply['error_code'], reply['retryable']) == (75, 'TIER_PARTIAL', True)
    detail = reply['detail']
    assert detail['raw_error_code'] == 'BAOSTOCK_GAP_UNRESOLVED'
    assert detail['incomplete_total'] == 5
    assert detail['incomplete'][0] == {'date': '2026-11-02', 'symbol': '688796'}
    assert all(key in detail for key in ('planned_days', 'planned_symbols', 'requested', 'empty_returns',
        'rows_written', 'upstream_empty_total', 'listing_source', 'listing_version', 'skipped_not_listed',
        'coverage', 'elapsed_seconds'))
    guardian_handler._json(reply)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and json.loads(lines[0]) == reply
    assert check_reply(code, json.loads(lines[0])) == []


def test_c817_real_empty_response_on_delivered_days():
    store, client, days = _c806_setup()
    raw = {v["symbol"]: v for v in map(json.loads,
        (Path(__file__).parent / "fixtures/daily_kline_c806/history.jsonl").read_text().splitlines())}
    assert raw["301139"]["rows"] == []
    client.query = lambda symbol, start, end: [
        parse_row(dict(zip(raw[symbol]["fields"], row)))
        for row in raw[symbol]["rows"] if start <= row[0] <= end
    ]
    result = run_daily_kline_fill(days[-1], store=store, client=client, trading_dates=days,
                                  expected_symbols=lambda day: {"301139", "688797"},
                                  depth=len(days), require_all=True)
    assert result["status"] == "COMPLETE"
    assert result["upstream_empty_total"] == 1
    assert result["upstream_empty"] == [{"symbol": "301139", "days": days}]
    assert result["incomplete_total"] == 0


def test_c817_request_failure_remains_incomplete():
    store, client, days = _c806_setup()
    def fail(*args):
        raise RuntimeError("request failed")
    client.query = fail
    result = run_daily_kline_fill(days[-1], store=store, client=client, trading_dates=days,
                                  expected_symbols=lambda day: {"301139", "688797"},
                                  depth=len(days), require_all=True)
    assert result["status"] == "INCOMPLETE"
    assert result["incomplete_total"] == len(days)
    assert result["upstream_empty_total"] == 0


def test_c827_empty_request_keeps_current_published_marker():
    store, client, days = _c806_setup()
    client.query = lambda symbol, start, end: []
    before = {day: store.read_versions(day) for day in days}
    published = []
    result = run_daily_kline_fill(days[-1], store=store, client=client, trading_dates=days,
        expected_symbols=lambda day: {'301139', '688797'}, depth=len(days), require_all=True,
        needs_publish=lambda day: False, on_complete=lambda r: published.append(r['publish_days']))
    assert result['status'] == 'COMPLETE'
    assert result['rows_written'] == 0
    assert published == []
    assert {day: store.read_versions(day) for day in days} == before
    assert result['published_partitions'] == 0 and result['publish_skipped'] is True


def test_c827_cli_failure_reply_includes_statistics(monkeypatch, capsys):
    from examples import backfill_td_daily_kline
    monkeypatch.delenv('TD_CONFIG', raising=False)
    backfill_td_daily_kline.main(['--date', '2026-11-06'])
    detail = json.loads(capsys.readouterr().out.splitlines()[-1])['detail']
    assert all(key in detail for key in ('planned_days', 'planned_symbols', 'requested', 'empty_returns',
        'rows_written', 'incomplete_total', 'upstream_empty_total', 'listing_source',
        'published_partitions', 'publish_skipped'))
    assert detail['incomplete_total'] is None and isinstance(detail['publish_skipped'], bool)


def test_resource_failure_reply_includes_publish_progress(monkeypatch, tmp_path):
    from examples import guardian_handler
    from engine_core.data_pipeline import daily_kline_fill as module
    from engine_core.data_pipeline.td_sink import TdResourceExhausted
    from engine_core.guardian.contract import check_reply

    store, client, days = _c806_setup()
    store.shard_events = [{"segments": 4}]
    def publish(root, store, days, expected, threshold, progress):
        progress.update(completed_days=1, published_days=1, current_day=days[1])
        raise TdResourceExhausted('Query memory exhausted')
    monkeypatch.setattr(module, 'publish_filled_days', publish)
    monkeypatch.setattr(module, 'load_listing_snapshot', lambda days: None)
    monkeypatch.setattr(module, 'make_expected_symbols', lambda *a, **kw: lambda day: {'688796', '688797'})
    run = module.run_daily_kline_fill_from_root
    monkeypatch.setattr(module, 'run_daily_kline_fill_from_root', lambda root, target, args: run(
        root, target, args={'fill_lease': 'off', 'history_min_trading_days': len(days)},
        store=store, client=client, trading_dates=days))
    code, reply = guardian_handler._run_daily_kline_fill({}, tmp_path, days[-1], {})
    assert (code, reply['error_code'], reply['retryable']) == (75, 'TD_RESOURCE_EXHAUSTED', True)
    assert reply['detail']['completed_days'] == reply['detail']['published_days'] == 1
    assert reply['detail']['current_day'] == days[1]
    assert all(k in reply['detail'] for k in ('planned_days', 'requested', 'rows_written'))
    assert reply['detail']['published_partitions'] == 1
    assert reply['detail']['publish_skipped'] is False
    assert reply['detail']['shard_events'] == [{"segments": 4}]
    assert check_reply(code, reply) == []
