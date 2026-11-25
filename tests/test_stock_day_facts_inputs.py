"""P1/C1: stock_day_facts records which daily_kline version it was built from, with the selector every derived
builder uses (select_best), so staleness can be judged and the chain converges (real captured rows, real catalog)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from td_sink_support import commit_day, load_real  # noqa: E402

from engine_core.data_pipeline.derived_daily import inputs_stale, select_best  # noqa: E402
from engine_core.data_pipeline.ops import build_stock_day_facts  # noqa: E402
from engine_core.data_pipeline.storage import ArtifactCatalog  # noqa: E402

DAY = "2026-10-26"
QUICK_RECAP_ROWS = load_real("formal")[1][:200]   # the 15:05 recap covers fewer symbols than the later Baostock day


def best_manifest(catalog, dataset, day):
    entries = catalog.read()["datasets"][dataset]["dates"][day]
    return json.loads(Path(select_best([e for e in entries if isinstance(e, dict)])["manifest"]).read_text())


def test_first_build_records_the_selected_input_version_and_is_not_stale(tmp_path):
    provisional = commit_day(tmp_path, DAY, "wencai")
    catalog = ArtifactCatalog(tmp_path)
    built = build_stock_day_facts(catalog, DAY, execute=True)
    assert built["status"] == "ok" and built["rows"] > 0
    manifest = best_manifest(catalog, "stock_day_facts", DAY)
    assert manifest["input_refs"] == [{"dataset": "daily_kline", "trade_date": DAY,
                                       "version": provisional["version"], "business_status": "PROVISIONAL"}]
    assert manifest["window"] == {"fixed_days": True}          # a one-day input must not be compared with a 120-day window
    assert inputs_stale(catalog.read(), "stock_day_facts", DAY, manifest) is False


def test_rebuilding_with_the_same_input_makes_no_new_version(tmp_path):
    commit_day(tmp_path, DAY, "wencai")
    catalog = ArtifactCatalog(tmp_path)
    first = build_stock_day_facts(catalog, DAY, execute=True)
    second = build_stock_day_facts(catalog, DAY, execute=True)
    assert second["version"] == first["version"]
    assert len(catalog.read()["datasets"]["stock_day_facts"]["dates"][DAY]) == 1


def test_a_better_daily_kline_makes_it_stale_and_the_rebuild_follows_the_new_version(tmp_path):
    commit_day(tmp_path, DAY, "wencai", rows=QUICK_RECAP_ROWS)
    catalog = ArtifactCatalog(tmp_path)
    build_stock_day_facts(catalog, DAY, execute=True)
    formal = commit_day(tmp_path, DAY, "baostock")                 # Baostock arrives later and outranks the quick recap
    catalog = ArtifactCatalog(tmp_path)
    old = best_manifest(catalog, "stock_day_facts", DAY)
    assert inputs_stale(catalog.read(), "stock_day_facts", DAY, old) is True
    build_stock_day_facts(catalog, DAY, execute=True)
    new = best_manifest(catalog, "stock_day_facts", DAY)
    assert new["input_refs"][0]["version"] == formal["version"] and new["version"] != old["version"]
    assert inputs_stale(catalog.read(), "stock_day_facts", DAY, new) is False


def test_it_uses_the_same_selector_as_the_staleness_check_not_the_newest_or_locate(tmp_path):
    commit_day(tmp_path, DAY, "baostock")                           # FORMAL, outranks
    commit_day(tmp_path, DAY, "wencai", rows=QUICK_RECAP_ROWS)      # committed later but lower quality
    catalog = ArtifactCatalog(tmp_path)
    chosen = select_best([e for e in catalog.read()["datasets"]["daily_kline"]["dates"][DAY] if isinstance(e, dict)])
    build_stock_day_facts(catalog, DAY, execute=True)
    manifest = best_manifest(catalog, "stock_day_facts", DAY)
    assert manifest["input_refs"][0]["version"] == chosen["version"] and chosen["business_status"] == "FORMAL"
    assert inputs_stale(catalog.read(), "stock_day_facts", DAY, manifest) is False


def test_missing_daily_kline_is_a_retryable_result_and_commits_nothing(tmp_path):
    catalog = ArtifactCatalog(tmp_path)
    built = build_stock_day_facts(catalog, DAY, execute=True)
    assert (built["status"], built["error"], built["retryable"]) == ("failed", "daily_kline_missing", True)
    assert "stock_day_facts" not in catalog.read().get("datasets", {})


def test_dry_run_commits_nothing(tmp_path):
    commit_day(tmp_path, DAY, "wencai")
    catalog = ArtifactCatalog(tmp_path)
    built = build_stock_day_facts(catalog, DAY, execute=False)
    assert built["status"] == "ok" and built["read_only"] is True and "version" not in built
    assert "stock_day_facts" not in catalog.read()["datasets"]


def test_the_postmarket_phase_records_the_same_inputs_as_the_registry_item(tmp_path, monkeypatch):
    # the phase builds stock_day_facts through JobRunner (Redis publish path); its manifest must carry the record too
    from test_core_data_ops import _seed_c325_calendar
    import engine_core.data_pipeline.ops as ops
    from engine_core.data_pipeline.ops import run_daily_phase

    monkeypatch.setitem(ops._PHASE_JOBS, "postmarket", ("daily_kline", "stock_day_facts"))   # only the step under test
    monkeypatch.setattr(ops, "run_derived_daily", lambda *_a, **_k: [])

    _seed_c325_calendar(tmp_path)
    formal = commit_day(tmp_path, DAY, "baostock")
    result = run_daily_phase("postmarket", DAY, root=tmp_path, adapters={}, trading_dates=None, write=True,
                             now=f"{DAY}T18:30:00+08:00", lock=False, fetch_daily_kline=False)
    job = next(item for item in result["jobs"] if item["job"] == "stock_day_facts")
    assert job["status"] in {"READY", "PARTIAL"} and job["row_count"] > 0
    catalog = ArtifactCatalog(tmp_path)
    manifest = best_manifest(catalog, "stock_day_facts", DAY)
    assert manifest["input_refs"][0]["version"] == formal["version"] and manifest["window"] == {"fixed_days": True}
    assert inputs_stale(catalog.read(), "stock_day_facts", DAY, manifest) is False
    again = build_stock_day_facts(catalog, DAY, execute=True)           # the reconcile path agrees: same version
    assert again["version"] == manifest["version"]
