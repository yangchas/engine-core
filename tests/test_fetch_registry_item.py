from examples.fetch_registry_item import fetch_item
from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.guardian.startup import boot_apply_plan


def test_theme_assignment_missing_inputs_is_retryable(tmp_path):
    result = fetch_item("stock_theme_assignment", "2026-10-20", tmp_path, execute=True)
    assert result["status"] == "failed"
    assert result["error"] == "trading_calendar_missing"
    assert result["retryable"] is True


def test_boot_apply_plan_records_manual_action_without_failure(tmp_path):
    result = boot_apply_plan("2026-10-20", [{"id": "stock_static", "action": "NEEDS_OPERATOR",
                            "partition": "2026-10-20", "reason": "manual_reference_import_required"}],
                             tmp_path, dry_run=False)
    assert result["status"] == "ok"
    assert result["detail"]["results"][0]["status"] == "needs_operator"
    assert not (tmp_path / "gaps.json").exists()


def test_shared_derivers_preserve_quality_and_caller_authority(tmp_path):
    from engine_core.data_pipeline.ops import derive_stock_day_facts
    from engine_core.prep_trade_day import build_and_commit_stock_theme_assignment

    facts, quality = derive_stock_day_facts(
        "2026-10-20",
        [{"symbol": "000001", "close": 10, "pre_close": 9, "amount": 12, "volume": 3}],
        {"source_version": "daily-v1"},
    )
    assert quality == "CLOSE_PRICE_PROXY"
    assert facts == [{"symbol": "000001", "trade_date": "2026-10-20", "code": None,
                      "universe_version": None, "close": 10, "pre_close": 9,
                      "pct_chg": None, "amount_yuan": 12, "volume_shares": 3,
                      "fact_source": "daily_kline", "quality": "CLOSE_PRICE_PROXY",
                      "source_version": "daily-v1"}]

    catalog = ArtifactCatalog(tmp_path)
    committed = build_and_commit_stock_theme_assignment(
        catalog, trade_date="2026-10-20", previous_trade_date="2026-10-19",
        pool_labels={}, reason_rows=[], static_themes={}, hot_plate_names={}, members={},
        trading_dates=["2026-10-19"], available_at="2026-10-21T09:00:00+08:00",
        source_versions={"pool": "pool-v1"}, as_of="2026-10-19T23:59:59+08:00",
        row_metadata={"late_audit_rebuild": True,
                      "authority": "NON_AUTHORITATIVE_AUDIT_ONLY"},
        manifest_metadata={"late_audit_rebuild": True,
                           "authority": "NON_AUTHORITATIVE_AUDIT_ONLY",
                           "report_consumed": False}, source="late_audit_rebuild")
    assert committed["row_count"] == 0
    assert committed["source_versions"] == {"pool": "pool-v1"}
    assert committed["late_audit_rebuild"] is True
    assert committed["authority"] == "LIVE"
    assert committed["declared_authority"] == "NON_AUTHORITATIVE_AUDIT_ONLY"


def test_theme_assignment_reads_pool_hot_view_when_catalog_missing(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from examples import fetch_registry_item as cli
    from engine_core import prep_trade_day, slices
    from engine_core.data_pipeline import limit_pool, limit_pool_hot_view as hot, trade_calendar

    pool_day, day = "2026-10-26", "2026-10-29"
    fixture = Path(__file__).parent / "fixtures/limit_pool/kaipan_pool_20261026.json"
    pages = json.loads(fixture.read_text())["pages"]
    part = limit_pool.build_kaipan(pool_day, {int(k): v for k, v in pages.items()})
    manifest = {**part.manifest, "version": "recorded-pool", "available_at": "2026-10-28T10:11:25+08:00",
                "quality": {**part.manifest["quality"], "status": "PARTIAL", "partial": True},
                "business_status": "PARTIAL"}

    class Redis:
        value = None

        def get(self, key):
            assert key == f"core:view:limit_up_pool:{pool_day}"
            return self.value

    redis = Redis()
    monkeypatch.setattr(hot, "_client", lambda: redis)
    monkeypatch.setattr(hot, "_td_store", lambda: (_ for _ in ()).throw(RuntimeError("no TD")))
    monkeypatch.setattr(cli, "_previous", lambda root, date: pool_day)
    original_load = cli._load
    monkeypatch.setattr(cli, "_load", lambda catalog, name, date:
                        original_load(catalog, name, date) if name == "limit_up_pool"
                        else ([], {"version": name}))
    monkeypatch.setattr(cli, "_manifest", lambda catalog, name, date:
                        (tmp_path / "registry", {"version": "registry"}) if name == "plate_registry" else None)
    monkeypatch.setattr(ArtifactCatalog, "read", lambda self:
                        {"datasets": {"limit_reasons": {"dates": {pool_day: []}}}})
    monkeypatch.setattr(ArtifactCatalog, "read_rows", lambda self, path: [])
    monkeypatch.setattr(trade_calendar.TradeCalendarStore, "trading_dates", lambda self, start, end: [pool_day])
    seen = []
    original_pit = slices._pit_rows
    def record_pit(rows, asof):
        seen.append((rows, asof))
        return original_pit(rows, asof)
    monkeypatch.setattr(slices, "_pit_rows", record_pit)
    monkeypatch.setattr(prep_trade_day, "build_and_commit_stock_theme_assignment",
                        lambda *args, **kwargs: {"row_count": len(kwargs["pool_labels"])})

    missing = fetch_item("stock_theme_assignment", day, tmp_path)
    assert missing["error"] == "inputs_missing:limit_up_pool"
    redis.value = json.dumps(hot.envelope(pool_day, part.rows, manifest))
    result = fetch_item("stock_theme_assignment", day, tmp_path)
    assert result["status"] == "ok"
    assert (part.rows, day + "T09:08:22+08:00") in seen
    rows, meta = cli._load_pool_hot(pool_day, day + "T09:08:22+08:00")
    assert rows == part.rows
    assert meta["status"] == "PARTIAL"
    for key in ("provider", "source", "quality", "business_status", "available_at", "version"):
        assert meta[key] == manifest[key]
    assert cli._load_pool_hot(pool_day, "2026-10-27T09:08:22+08:00") is None
