from examples.fetch_registry_item import fetch_item
from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.guardian.startup import boot_apply_plan


def test_missing_daily_kline_is_retryable(tmp_path):
    result = fetch_item("stock_day_facts", "2026-10-20", tmp_path, execute=True)
    assert result["status"] == "failed"
    assert result["retryable"] is True
    assert result["error"] == "daily_kline_missing"


def test_stock_day_facts_dry_run_and_execute_use_catalog_rows(tmp_path):
    catalog = ArtifactCatalog(tmp_path)
    source = [{"symbol": "000001", "close": 10, "pre_close": 9,
               "pct_chg": 11.1, "amount_yuan": 123, "volume": 45}]
    catalog.commit("daily_kline", "2026-10-20", source,
                   {"available_at": "2026-10-20T16:00:00+08:00", "quality": {"status": "READY"}},
                   source="test-fixture")
    dry = fetch_item("stock_day_facts", "2026-10-20", tmp_path)
    assert dry["status"] == "ok" and dry["read_only"] is True
    assert catalog.locate("stock_day_facts", "2026-10-20") is None
    done = fetch_item("stock_day_facts", "2026-10-20", tmp_path, execute=True)
    assert done["status"] == "ok" and done["read_only"] is False
    rows = catalog.read_rows(catalog.locate("stock_day_facts", "2026-10-20"))
    assert rows[0]["quality"] == "CLOSE_PRICE_PROXY"
    assert rows[0]["source_version"] == catalog.read()["datasets"]["daily_kline"]["dates"]["2026-10-20"][0]["version"]
    assert "p0932" not in rows[0]


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
