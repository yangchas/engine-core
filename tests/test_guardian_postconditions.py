# DATA: real:core_artifacts/c651/stock_static rows, td/outputs/reference_snapshot_plate_members_latest.json (derived variant: row_count=0 for the empty case); spec:calendar copy of ~/core-data/trade_calendar for the limit-pool case
import gzip
import json
from pathlib import Path

from engine_core.guardian.commands import validate_handler_postcondition


def _real_rows(count=3):
    # real stock_static rows (C651 capture of the 2026-10-29 partition); these tests only need a non-empty real partition
    path = (Path(__file__).parent / "fixtures" / "real" / "core_artifacts" / "c651" / "stock_static"
            / "trade_date=2026-10-29" / "source=baostock_stock_basic" / "sha256-d2f57645aafb76b7" / "rows.jsonl.gz")
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(next(handle)) for _ in range(count)]


REAL_ROWS = _real_rows()


def _catalog(root: Path, dataset: str, day: str, rows=(), **manifest):
    from engine_core.data_pipeline.storage import ArtifactCatalog

    ArtifactCatalog(root).commit(dataset, day, rows, manifest, source="test")


def test_kaipan_fetch_requires_nonempty_previous_day_limit_pool(tmp_path, monkeypatch):
    import shutil
    from engine_core.data_pipeline import limit_pool_hot_view as hot
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from test_limit_pool_hot_view import RedisSpy
    real_root = Path.home() / "core-data"
    shutil.copytree(real_root / "trade_calendar", tmp_path / "trade_calendar")
    redis = RedisSpy()
    monkeypatch.setattr(hot, "_client", lambda: redis)
    monkeypatch.setattr(hot, "_td_store", lambda: (_ for _ in ()).throw(RuntimeError("missing")))
    assert validate_handler_postcondition("kaipan_fetch", "2026-11-11", tmp_path) == (False, "limit_up_pool_missing_or_empty")
    catalog = ArtifactCatalog(real_root)
    path = catalog.locate("limit_up_pool", "2026-11-10")
    manifest = json.loads(path.read_text())
    value = hot.envelope("2026-11-10", catalog.read_rows(path), manifest)
    redis.writes["core:view:limit_up_pool:2026-11-09"] = json.dumps(value)
    assert not validate_handler_postcondition("kaipan_fetch", "2026-11-11", tmp_path)[0]
    redis.writes["core:view:limit_up_pool:2026-11-10"] = json.dumps(value)
    assert validate_handler_postcondition("kaipan_fetch", "2026-11-11", tmp_path) == (True, "")


def test_daily_kline_postcondition_reads_catalog(tmp_path, monkeypatch):
    monkeypatch.delenv("TD_CONFIG", raising=False)
    assert not validate_handler_postcondition("postmarket_data", "2026-10-20", tmp_path)[0]
    _catalog(tmp_path, "daily_kline", "2026-10-20", REAL_ROWS)
    assert validate_handler_postcondition("baostock_probe", "2026-10-20", tmp_path) == (True, "")


def test_calendar_postcondition_requires_lit_coverage(tmp_path):
    calendar = tmp_path / "trade_calendar" / "calendar.json"
    calendar.parent.mkdir(parents=True)
    calendar.write_text(json.dumps({"schema": "CoreTradeCalendarMatrixV1", "lit_from": "2026-10-01",
                                    "lit_to": "2026-10-30", "days": []}))
    assert validate_handler_postcondition("calendar_refresh", "2026-10-20", tmp_path) == (True, "")
    assert not validate_handler_postcondition("calendar_refresh", "2026-11-01", tmp_path)[0]


def test_premarket_requires_reference_manifests(tmp_path):
    assert not validate_handler_postcondition("premarket_data", "2026-10-20", tmp_path)[0]
    _catalog(tmp_path, "symbol_universe", "2026-10-20", REAL_ROWS)
    assert validate_handler_postcondition("premarket_data", "2026-10-20", tmp_path) == (True, "")


def test_partial_daily_kline_is_neither_present_nor_a_passed_postcondition(tmp_path, monkeypatch):
    """Real shape from 2026-10-21: a timed-out Baostock attempt committed 1774 of 5224 rows (coverage 0.34)."""
    monkeypatch.delenv("TD_CONFIG", raising=False)
    from datetime import datetime
    from engine_core.guardian.registry import catalog_partition_present
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from conftest import read_real_artifact, seed_real_symbol_universe

    day = "2026-10-23"
    _, rows = read_real_artifact("daily_kline", day, "formal")
    from conftest import use_declared_time_as_test_commit_clock
    catalog_writer = use_declared_time_as_test_commit_clock(ArtifactCatalog(tmp_path))
    seed_real_symbol_universe(catalog_writer, day)
    catalog_writer.commit("daily_kline", day, rows[:1774],
        {"trade_date": day, "provider": "baostock", "available_at": f"{day}T17:41:00+08:00"},
        source="baostock")
    catalog = ArtifactCatalog(tmp_path).read()
    now = datetime.fromisoformat(f"{day}T18:30:00+08:00")
    assert not catalog_partition_present(catalog, "daily_kline", day, now)
    ok, reason = validate_handler_postcondition("baostock_probe", day, tmp_path)
    assert not ok and reason.startswith("daily_kline_partial:0.340")
    catalog_writer.commit("daily_kline", day, rows,
        {"trade_date": day, "provider": "baostock", "available_at": f"{day}T18:10:00+08:00"},
        source="baostock")
    assert catalog_partition_present(ArtifactCatalog(tmp_path).read(), "daily_kline", day, now)
    assert validate_handler_postcondition("baostock_probe", day, tmp_path) == (True, "")


def test_daily_kline_td_authority_bypasses_catalog(tmp_path, monkeypatch):
    from engine_core.data_pipeline import td_sink
    from engine_core.data_pipeline.storage import ArtifactCatalog

    day = "2026-11-24"
    closed = []
    class Store:
        def __init__(self, database):
            assert database == "core_data"
        def day_counts(self, dates):
            assert dates == [day]
            return {day: 5224}
        def close(self):
            closed.append(True)
    monkeypatch.setenv("TD_CONFIG", "configured")
    monkeypatch.setattr(td_sink, "TaosStore", Store)
    monkeypatch.setattr(ArtifactCatalog, "locate", lambda *args: (_ for _ in ()).throw(AssertionError("catalog consulted")))
    for name in ("baostock_probe", "postmarket_data"):
        assert validate_handler_postcondition(name, day, tmp_path) == (True, "")
    assert len(closed) == 2
    assert not (tmp_path / "catalog.json").exists()


def test_daily_kline_td_under_threshold_falls_back_to_catalog(tmp_path, monkeypatch):
    from engine_core.data_pipeline import td_sink

    day = "2026-11-24"
    closed = []
    class Store:
        def __init__(self, database):
            assert database == "core_data"
        def day_counts(self, dates):
            assert dates == [day]
            return {day: 4499}
        def close(self):
            closed.append(True)
    monkeypatch.setenv("TD_CONFIG", "configured")
    monkeypatch.setattr(td_sink, "TaosStore", Store)
    assert validate_handler_postcondition("baostock_probe", day, tmp_path) == (False, "daily_kline_manifest_missing")
    assert closed == [True]


def test_hot_plate_history_postcondition_requires_each_prior_session_saved_or_gapped(
        tmp_path, monkeypatch):
    from fnmatch import fnmatch
    from engine_core.data_pipeline import limit_pool_hot_view, redis_day_view
    from engine_core.data_pipeline.hot_plates_gaps import history_event_id, record_hot_plates_gap
    from engine_core.data_pipeline.trade_calendar import TradeCalendarStore

    fixture = Path(__file__).parent / "fixtures" / "real"
    view = json.loads((fixture / "c1094" / "view.json").read_bytes())
    gap = json.loads((fixture / "c1106" / "row.jsonl").read_bytes())
    dates = [gap["trade_date"], "2026-11-12", view["trade_date"], "2026-11-16"]
    class Redis:
        def __init__(self):
            self.values = {}
        def get(self, key):
            return self.values.get(key)
        def scan_iter(self, match, count=100):
            return [key for key in self.values if fnmatch(key, match)]
        def set(self, key, value, ex=None, nx=False):
            self.values[key] = value
            return True
    redis = Redis()
    monkeypatch.setattr(limit_pool_hot_view, "_client", lambda: redis)
    monkeypatch.setattr(TradeCalendarStore, "all_trading_dates", lambda _self: dates)
    monkeypatch.setattr(redis_day_view, "hot_view_expire_at", lambda day: 4070908800)
    datasets = ["hot_plates_history", "hot_plates_history_days=3"]
    assert not validate_handler_postcondition("kaipan_hot_capture", dates[-1], tmp_path, datasets)[0]
    redis.values[f"core:cache:data:hot_plates_gaps:{gap['trade_date']}:{gap['event_id']}"] = json.dumps(gap)
    assert not validate_handler_postcondition("kaipan_hot_capture", dates[-1], tmp_path, datasets)[0]
    record_hot_plates_gap(tmp_path, dates[1], history_event_id(dates[1]),
                          gap["scheduled_at"], gap["deadline"], gap["observed_at"], gap["gap_reason"])
    assert not validate_handler_postcondition("kaipan_hot_capture", dates[-1], tmp_path, datasets)[0]
    view["event_id"] = history_event_id(view["trade_date"])
    redis.values[f"core:cache:data:hot_plates:{view['trade_date']}:{view['version']}"] = json.dumps(view)
    assert validate_handler_postcondition("kaipan_hot_capture", dates[-1], tmp_path, datasets) == (True, "")


MARKER = json.loads((Path(__file__).parent / "fixtures" / "real" / "td" / "outputs"
                     / "reference_snapshot_plate_members_latest.json").read_text(encoding="utf-8"))["value"]


class _Snapshots:
    """Stands in for the TD ReferenceSnapshotStore: it returns the REAL read_latest result recorded on 2026-11-04."""

    marker = MARKER

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read_latest(self, dataset):
        assert dataset == "plate_members"
        return self.marker


def _td_snapshots(monkeypatch, marker):
    import engine_core.data_pipeline.reference_snapshot_td as module
    monkeypatch.setenv("TD_CONFIG", "/nonexistent")
    store = type("Store", (_Snapshots,), {"marker": marker})
    monkeypatch.setattr(module, "ReferenceSnapshotStore", store)


def test_plate_members_is_judged_on_the_td_snapshot_not_a_catalog_partition(tmp_path, monkeypatch):
    _td_snapshots(monkeypatch, MARKER)
    assert validate_handler_postcondition("kaipan_fetch", MARKER["trade_date"], tmp_path, ["plate_members"]) == (True, "")


def test_plate_members_snapshot_of_another_day_or_empty_fails(tmp_path, monkeypatch):
    _td_snapshots(monkeypatch, MARKER)
    assert validate_handler_postcondition("kaipan_fetch", "2026-11-02", tmp_path, ["plate_members"]) == (
        False, f"plate_members_snapshot_stale:{MARKER['trade_date']}")
    _td_snapshots(monkeypatch, {**MARKER, "row_count": 0})     # derived from the real record: only row_count changed
    assert validate_handler_postcondition("kaipan_fetch", MARKER["trade_date"], tmp_path, ["plate_members"]) == (
        False, "plate_members_missing_or_empty")


def test_hot_plates_postcondition_replays_real_redis_and_empty_sources(tmp_path, monkeypatch):
    import fnmatch
    import shutil
    from engine_core.data_pipeline import cold_archive, limit_pool_hot_view
    from engine_core.data_pipeline.storage import ArtifactCatalog

    captured = json.loads((Path(__file__).parent / 'fixtures/real/c1417/excerpt.json').read_text())
    shutil.copytree(Path.home() / 'core-data/trade_calendar', tmp_path / 'trade_calendar')
    class RedisReplay:
        def scan_iter(self, match, count=100):
            return iter(k for k in captured['snapshots'] if fnmatch.fnmatch(k, match))
        def get(self, key):
            return json.dumps(captured['snapshots'][key])
    class EmptyTD:
        def latest_version(self, dataset, day):
            assert (dataset, day) == ('hot_plates', captured['empty_date'])
            return captured['empty_td_latest_version']
        def close(self):
            pass
    monkeypatch.setattr(limit_pool_hot_view, '_client', lambda: RedisReplay())
    monkeypatch.setattr(cold_archive, 'TdStore', EmptyTD)
    assert not captured['catalog_2026_11_16']
    assert validate_handler_postcondition('kaipan_fetch', captured['data_date'], tmp_path, ['hot_plates']) == (True, '')
    monkeypatch.setattr(ArtifactCatalog, 'locate', lambda *args: (_ for _ in ()).throw(AssertionError('catalog consulted')))
    assert validate_handler_postcondition('kaipan_fetch', captured['data_date'], tmp_path, ['hot_plates']) == (True, '')
    assert captured['empty_redis_key_count'] == 0
    assert validate_handler_postcondition('kaipan_fetch', captured['empty_date'], tmp_path, ['hot_plates']) == (False, 'hot_plates_missing_or_empty')
