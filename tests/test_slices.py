from engine_core.slices import SliceReader, SliceResult


def test_slice_reader_uses_core_provider_once_and_rejects_future_data():
    calls = []

    def core_provider(reader, request):
        calls.append(request)
        return SliceResult.create(
            [{"symbol": "600001", "event_time": "2026-10-15T09:00:00+08:00",
              "available_at": "2026-10-15T09:01:00+08:00"}],
            {"slice_id": "daily_history", "trade_date": request.trade_date,
             "asof": request.asof, "source": "core_data", "version": "v1",
             "built_at": "2026-10-15T10:00:00+08:00",
             "available_at": "2026-10-15T09:01:00+08:00", "quality": {},
             "status": "READY", "lineage": []},
        )

    reader = SliceReader({"daily_history": [core_provider]}, snapshot_id="test")
    first = reader.get("daily_history", trade_date="2026-10-15",
                       asof="2026-10-15T09:02:00+08:00")
    second = reader.get("daily_history", trade_date="2026-10-15",
                        asof="2026-10-15T09:02:00+08:00")
    assert first is second
    assert len(calls) == 1
    assert first.meta["source"] == "core_data"


def test_slice_provider_priority_and_pit_filtering(tmp_path):
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from engine_core.slices import SliceProviders

    catalog = ArtifactCatalog(tmp_path / "core")
    catalog.commit("stock_static", "2026-10-15", [
        {"symbol": "600001", "event_time": "2026-10-14T00:00:00+08:00",
         "available_at": "2026-10-14T01:00:00+08:00", "name": "Core"},
        {"symbol": "600002", "event_time": "2026-10-14T00:00:00+08:00",
         "available_at": "2026-10-15T09:03:00+08:00", "name": "Late"},
    ], {"available_at": "2026-10-14T01:00:00+08:00",
        "point_in_time_date": "2026-10-14"}, source="test")

    class Redis:
        def hgetall(self, key):
            return {"600001": '{"name":"Legacy"}'}

    providers = SliceProviders(catalog=catalog, redis=Redis(), file_fallback=False)
    reader = SliceReader(providers.providers())
    result = reader.get("stock_name_map", trade_date="2026-10-15",
                        asof="2026-10-15T09:02:00+08:00")
    assert result.meta["source"] == "core_data"
    assert result.meta["pit_date"] == "2026-10-14"
    assert result.meta["status"] == "PARTIAL"
    assert [row["symbol"] for row in result.rows] == ["600001"]


def test_pit_rejects_future_available_at_even_when_event_time_is_missing_and_normalizes_naive():
    from engine_core.slices import _pit_rows

    rows, unknown = _pit_rows([
        {"symbol": "600001", "available_at": "2026-10-16T09:27:00"},
        {"symbol": "600002", "event_time": "2026-10-16T09:00:00",
         "available_at": "2026-10-16T09:25:00"},
        {"symbol": "600003", "value": 1},
    ], "2026-10-16T09:26:00+08:00")

    assert [row["symbol"] for row in rows] == ["600002", "600003"]
    assert unknown is True

    only_available, unknown = _pit_rows(
        [{"symbol": "600004", "available_at": "2026-10-16T09:25:00"}],
        "2026-10-16T09:26:00+08:00")
    assert [row["symbol"] for row in only_available] == ["600004"]
    assert unknown is False


def test_daily_history_legacy_adapter_decodes_bytes_symbols():
    import json
    import zlib
    from engine_core.slices import SliceProviders, SliceRequest

    class Pipe:
        def __init__(self): self.keys = []
        def get(self, key): self.keys.append(key); return self
        def execute(self):
            return [zlib.compress(json.dumps([{"time": "2026-10-15", "close": 12.3}]).encode()).hex().encode()]

    class Redis:
        def __init__(self): self.pipe = Pipe()
        def hkeys(self, key): return [b"000001"]
        def pipeline(self): return self.pipe

    redis = Redis()
    providers = SliceProviders(redis=redis, file_fallback=False)
    request = SliceRequest("daily_history", "2026-10-16", "2026-10-16T09:26:00+08:00",
                           {"prior_trade_date": "2026-10-15"})
    rows = providers._legacy_rows(request)
    assert redis.pipe.keys == ["cache:kline:d:000001"]
    assert rows[0]["symbol"] == "000001"
    assert rows[0]["date"] == "2026-10-15"


def test_previous_trade_date_uses_observed_core_dates_and_fails_closed():
    from engine_core.slices import previous_trade_date

    class Catalog:
        def __init__(self, dates): self.dates = dates
        def read(self): return {"datasets": {"daily_kline": {"dates": {day: [] for day in self.dates}}}}

    assert previous_trade_date(Catalog(["2026-10-14", "2026-10-16"]), "2026-10-17") == "2026-10-16"
    assert previous_trade_date(Catalog([]), "2026-10-17") is None


def test_legacy_source_and_file_fallback_are_explicit(tmp_path):
    from engine_core.slices import SliceProviders

    class Redis:
        def hgetall(self, key):
            return {"600001": '{"lb_days":2}'} if key.endswith("2026-10-14") else {}

    file_path = tmp_path / "fallback.json"
    file_path.write_text('[{"symbol":"600002","event_time":"2026-10-14T00:00:00+08:00",'
                         '"available_at":"2026-10-14T01:00:00+08:00"}]')
    reader = SliceReader(SliceProviders(redis=Redis(), file_fallback=False).providers())
    missing = reader.get("yest_limit_pool", trade_date="2026-10-15",
                         asof="2026-10-15T09:02:00+08:00", source_date="2026-10-14",
                         fallback_file=str(file_path))
    assert missing.meta["status"] == "PARTIAL"
    assert missing.meta["source"] == "legacy_redis"
    assert missing.meta["reason"] == "NO_ASOF"

    enabled = SliceReader(SliceProviders(redis=None, file_fallback=True).providers())
    fallback = enabled.get("yest_limit_pool", trade_date="2026-10-15",
                           asof="2026-10-15T09:02:00+08:00", source_date="2026-10-14",
                           fallback_file=str(file_path))
    assert fallback.meta["source"] == "FILE_FALLBACK"
    assert fallback.meta["status"] == "PARTIAL"


def test_reference_import_dry_run_and_execute(tmp_path):
    import importlib.util
    from engine_core.data_pipeline.storage import ArtifactCatalog

    path = tmp_path / "input.csv"
    path.write_text("symbol,name\n600001,样例\n", encoding="utf-8")
    module_path = __import__("pathlib").Path(__file__).parents[1] / "examples/import_reference_to_core_store.py"
    spec = importlib.util.spec_from_file_location("slice_import_tool", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    catalog = ArtifactCatalog(tmp_path / "store")
    mapping = [f"stock_static={path}"]
    preview = module.import_files(mapping, catalog=catalog, trade_date="2026-10-15",
                                  as_of="2026-10-15T09:00:00+08:00", pit_date="2026-10-14")
    assert preview[0]["version"] is None
    assert catalog.locate("stock_static", "2026-10-15") is None
    committed = module.import_files(mapping, catalog=catalog, trade_date="2026-10-15",
                                    as_of="2026-10-15T09:00:00+08:00", pit_date="2026-10-14",
                                    execute=True)
    manifest = __import__("json").loads(catalog.locate("stock_static", "2026-10-15").read_text())
    assert committed[0]["version"]
    assert manifest["provenance"] == "import_from_validation"
    assert manifest["source_sha256"] == preview[0]["source_sha256"]


def test_reference_import_flattens_point_in_time_kaipan_reasons(tmp_path):
    import importlib.util
    from engine_core.data_pipeline.storage import ArtifactCatalog

    source = tmp_path / "kaipan" / "reasons"
    source.mkdir(parents=True)
    (source / "600001.json").write_text(
        '{"StockID":"600001","List":[{"ZSCode":["801001"],"PZSCode":"803023",'
        '"Reason":"样例原因","Date":"2026-10-14","SCLT":"1","Type":"0"}]}',
        encoding="utf-8")
    module_path = __import__("pathlib").Path(__file__).parents[1] / "examples/import_reference_to_core_store.py"
    spec = importlib.util.spec_from_file_location("slice_import_tool_dir", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    catalog = ArtifactCatalog(tmp_path / "store")
    module.import_files([f"limit_reasons={source.parent}"], catalog=catalog,
                        trade_date="2026-10-14", as_of="2026-10-14T18:00:00+08:00",
                        pit_date="2026-10-14", execute=True)
    manifest = catalog.locate("limit_reasons", "2026-10-14")
    row = catalog.read_rows(manifest)[0]
    assert row["symbol"] == "600001"
    assert row["ZSCode"] == ["801001"]
    assert row["PZSCode"] == "803023"
    assert row["Reason"] == "样例原因"
