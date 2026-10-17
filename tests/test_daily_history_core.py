from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.slices import SliceProviders, SliceReader


def test_daily_history_reads_pit_eligible_core_days_before_legacy(tmp_path):
    catalog = ArtifactCatalog(tmp_path)
    for day, close, available_at in (
        ("2026-10-14", 10.0, "2026-10-14T17:30:00+08:00"),
        ("2026-10-15", 11.0, "2026-10-15T17:30:00+08:00"),
        ("2026-10-16", 12.0, "2026-10-16T17:30:00+08:00"),
    ):
        catalog.commit("daily_kline", day, [{"date": day, "symbol": "000001", "close": close}],
                       {"trade_date": day, "available_at": available_at, "source": "baostock"},
                       source="baostock")
    catalog.commit("daily_indicators", "2026-10-15", [{"date": "2026-10-15", "symbol": "000001",
                   "ma5": 10.0, "ma10": None, "source": "core_derived"}],
                   {"trade_date": "2026-10-15", "available_at": "2026-10-15T17:30:00+08:00"},
                   source="core_derived")
    providers = SliceProviders(catalog=catalog, redis=None)
    reader = SliceReader(providers.providers())

    result = reader.get("daily_history", trade_date="2026-10-17",
                        asof="2026-10-16T16:00:00+08:00", prior_trade_date="2026-10-16")

    assert result.meta["source"] == "core_data"
    assert [row["date"] for row in result.rows] == ["2026-10-14", "2026-10-15"]
    assert all(row["source"] == "baostock" for row in result.rows)
    assert result.meta["available_at"] == "2026-10-15T17:30:00+08:00"
    assert result.rows[-1]["ma5"] == 10.0
    assert result.rows[-1]["indicator_source"] == "core_derived"


def test_daily_indicators_use_legacy_formulas_and_keep_short_windows_missing():
    from engine_core.daily_history import calculate_daily_indicators

    rows = []
    for index in range(25):
        rows.append({"date": f"2026-09-{index + 1:02d}", "symbol": "000001",
                     "close": 10.0 + index, "pct_chg": 1.0, "turnover_pct": 2.0})
    indicator = calculate_daily_indicators("000001", rows)

    assert indicator["ma5"] == 32.0
    assert indicator["ma10"] == 29.5
    assert indicator["ma20"] == 24.5
    assert indicator["change_pct_5d"] == 17.24
    assert indicator["avg_turnover_5d"] == 2.0
    assert indicator["vol_ratio"] == 0.95
    assert indicator["rsi_6"] == 100.0
    assert calculate_daily_indicators("000001", rows[:4])["ma5"] is None
    assert calculate_daily_indicators("000001", rows[:4])["change_pct_5d"] is None


def test_daily_history_hot_view_keeps_five_bars_and_latest_indicators():
    import gzip
    import json
    from engine_core.daily_history import write_daily_history_cache

    class Redis:
        values = {}

        def set(self, key, value, ex):
            self.values[key] = value

    redis = Redis()
    rows = [{"symbol": "000001", "date": f"2026-10-{day:02d}", "close": day,
             "available_at": f"2026-10-{day:02d}T17:30:00+08:00",
             **({"ma20": 8.0, "bias_20": 25.0} if day == 16 else {})}
            for day in range(1, 17)]

    assert write_daily_history_cache(redis, "2026-10-16", rows,
                                     available_at="2026-10-16T17:30:00+08:00")
    payload = json.loads(gzip.decompress(redis.values[
        "core:cache:data:daily_history:2026-10-16"]))

    assert [row["date"] for row in payload["rows"]] == [
        "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]
    assert payload["rows"][-1]["ma20"] == 8.0
    assert all("ma20" not in row for row in payload["rows"][:-1])


def test_legacy_daily_history_fallback_is_marked_degraded():
    class Redis:
        def hkeys(self, _key):
            return [b"000001"]

        def pipeline(self):
            class Pipe:
                def get(self, _key):
                    return self

                def execute(self):
                    import json, zlib
                    return [zlib.compress(json.dumps([{"time": "2026-10-15", "close": 11}]).encode()).hex()]
            return Pipe()

    providers = SliceProviders(catalog=ArtifactCatalog("/missing-core-data"), redis=Redis())
    result = SliceReader(providers.providers()).get(
        "daily_history", trade_date="2026-10-16", asof="2026-10-16T20:00:00+08:00",
        prior_trade_date="2026-10-15")

    assert result.meta["status"] == "DEGRADED"
    assert result.meta["source"] == "legacy_redis"
    assert result.meta["reason"] == "LEGACY_DAILY_HISTORY_DEGRADED"
    assert result.rows[0]["available_at"] is None


def test_history_import_checkpoints_completed_symbol_batches(tmp_path):
    from engine_core.daily_history import backfill_daily_history

    catalog = ArtifactCatalog(tmp_path / "core")
    catalog.commit("daily_kline", "2026-10-16", [
        {"date": "2026-10-16", "symbol": "000001", "close": 10.0},
        {"date": "2026-10-16", "symbol": "000002", "close": 20.0},
    ], {"available_at": "2026-10-16T17:30:00+08:00"}, source="baostock")

    class Client:
        def __init__(self):
            self.module = self
            self.queries = []

        def login(self):
            return None

        def logout(self):
            return None

        def query(self, symbol, start, end):
            self.queries.append(symbol)
            return [{"date": "2026-10-15", "symbol": symbol, "close": 10.0,
                     "pre_close": 9.0, "volume": 1.0, "amount_yuan": 100.0,
                     "turnover_pct": 1.0, "pct_chg": 1.0}]

    class Redis:
        def __init__(self):
            self.values = {}

        def set(self, key, value, ex):
            self.values[key] = (value, ex)

        def get(self, key):
            entry = self.values.get(key)
            return entry[0] if entry else None

    client = Client()
    redis = Redis()
    checkpoint = tmp_path / "checkpoints"
    report = backfill_daily_history(catalog, start="2026-10-15", end="2026-10-15",
                                    checkpoint_root=checkpoint, client=client, batch_size=1,
                                    redis=redis)
    assert report["completed_symbols"] == report["symbol_count"] == 2
    assert report["days_committed"] == 1
    assert len(client.queries) == 2
    assert "core:cache:data:daily_history:2026-10-15" in redis.values
    import gzip, json
    batch_checkpoint = next((checkpoint / "range-2026-10-15-2026-10-15").glob("symbols-*.json.gz"))
    saved_batch = json.loads(gzip.decompress(batch_checkpoint.read_bytes()))
    assert saved_batch["source"] == "baostock"
    assert saved_batch["parameters"]["start"] == "2026-10-15"
    assert saved_batch["parameters"]["end"] == "2026-10-15"
    assert saved_batch["fetched_at"]
    assert catalog.locate("daily_indicators", "2026-10-15") is not None
    cached = SliceReader(SliceProviders(catalog=catalog, redis=redis).providers()).get(
        "daily_history", trade_date="2026-10-16", asof="2026-10-16T20:00:00+08:00",
        prior_trade_date="2026-10-15")
    assert cached.meta["version"] == "redis_hot_view"
    assert cached.meta["source"] == "core_data"
    assert cached.rows[0]["indicator_source"] == "core_derived"
    long_window = SliceReader(SliceProviders(catalog=catalog, redis=redis).providers()).get(
        "daily_history", trade_date="2026-10-16", asof="2026-10-16T20:00:00+08:00",
        prior_trade_date="2026-10-15", history_days=6)
    assert long_window.meta["version"] == "multi_date"
    assert long_window.rows[0]["indicator_source"] == "core_derived"

    resumed = Client()
    backfill_daily_history(catalog, start="2026-10-15", end="2026-10-15",
                           checkpoint_root=checkpoint, client=resumed, batch_size=1)
    assert resumed.queries == []

    wider_range = Client()
    backfill_daily_history(catalog, start="2026-10-14", end="2026-10-15",
                           checkpoint_root=checkpoint, client=wider_range, batch_size=1)
    assert wider_range.queries == ["000001", "000002"]


def test_baostock_range_client_sets_socket_timeout_and_reconnects(monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace
    from engine_core.daily_history import BaostockRangeClient
    root = ModuleType("baostock")
    common = ModuleType("baostock.common")
    context = ModuleType("baostock.common.context")
    root.__path__ = []
    common.__path__ = []
    root.common = common
    common.context = context
    monkeypatch.setitem(sys.modules, "baostock", root)
    monkeypatch.setitem(sys.modules, "baostock.common", common)
    monkeypatch.setitem(sys.modules, "baostock.common.context", context)

    class Socket:
        def __init__(self):
            self.timeout = None
            self.closed = False

        def settimeout(self, value):
            self.timeout = value

        def close(self):
            self.closed = True

    sockets = []

    class Module:
        def login(self):
            sock = Socket()
            sockets.append(sock)
            context.default_socket = sock
            return SimpleNamespace(error_code="0")

    context.default_socket = None
    client = BaostockRangeClient(Module(), read_timeout=12.0)
    client.login()
    first = context.default_socket
    assert first.timeout == 12.0
    client.reconnect()
    assert first.closed is True
    assert context.default_socket is sockets[1]
    assert sockets[1].timeout == 12.0
