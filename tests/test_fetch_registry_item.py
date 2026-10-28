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
