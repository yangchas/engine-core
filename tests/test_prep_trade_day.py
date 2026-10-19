import json

from engine_core.prep_trade_day import build_prep_report, previous_trade_date
from examples.core_guardian import HANDLER_IMPLEMENTATIONS
from examples.run_data_jobs import _plate_codes
from engine_core.data_pipeline.storage import ArtifactCatalog


def test_all_guardian_handlers_have_explicit_core_mapping_or_not_implemented():
    expected = {
        "night_report", "premarket_data", "kaipan_fetch", "auction_watch",
        "kaipan_hot_capture", "snapshot_check", "auction_report", "open_report",
        "intraday_data", "postmarket_data", "baostock_probe", "close_capture", "close_report",
    }
    assert set(HANDLER_IMPLEMENTATIONS) == expected
    assert all(value and value != "" for value in HANDLER_IMPLEMENTATIONS.values())
    assert "NOT_IMPLEMENTED" in HANDLER_IMPLEMENTATIONS.values()
    assert HANDLER_IMPLEMENTATIONS["premarket_data"] == "examples/prep_trade_day.py:premarket_and_auction_baseline"


def test_previous_trade_date_uses_calendar_or_committed_daily_kline(tmp_path):
    calendar = tmp_path / "trade_dates.json"
    calendar.write_text(json.dumps(["2026-10-16", "2026-10-19"]), encoding="utf-8")
    assert previous_trade_date(tmp_path, "2026-10-19") == "2026-10-16"
    calendar.unlink()
    assert previous_trade_date(tmp_path, "2026-10-19", known_trade_dates=["2026-10-15", "2026-10-16"]) == "2026-10-16"
    assert previous_trade_date(tmp_path, "2026-10-19", known_trade_dates=[]) is None


def test_dry_run_reports_missing_without_mutating_artifact_root(tmp_path):
    report = build_prep_report(tmp_path, "2026-10-19", write=False)
    assert report["mode"] == "DRY_RUN"
    assert {item["status"] for item in report["steps"]} <= {
        "READY", "PARTIAL", "MISSING", "SKIPPED_REASON"
    }
    assert not (tmp_path / "catalog.json").exists()
    assert all(item["elapsed_ms"] >= 0 for item in report["steps"])


def test_kaipan_adapter_reads_plate_registry_from_core_catalog(tmp_path):
    catalog = ArtifactCatalog(tmp_path)
    catalog.commit("plate_registry", "2026-10-16",
                   [{"plate_code": "880001"}, {"plate_code": "880002"}],
                   {"trade_date": "2026-10-16", "row_count": 2}, source="fixture")
    assert _plate_codes(str(tmp_path), "2026-10-19") == ["880001", "880002"]


def test_prep_baseline_target_is_report_day(tmp_path, monkeypatch):
    import engine_core.prep_trade_day as prep

    (tmp_path / "trade_dates.json").write_text(
        json.dumps(["2026-10-16", "2026-10-19"]), encoding="utf-8")
    calls = []
    def fake_run(_repo, script, args):
        calls.append((script, list(args)))
        return 1, "fixture_missing"
    monkeypatch.setattr(prep, "_run", fake_run)
    report = prep.build_prep_report(tmp_path, "2026-10-19", write=True,
                                    asof="2026-10-19T08:00:00+08:00")
    baseline_calls = [(script, args) for script, args in calls if script == "build_auction_baseline.py"]
    universe_calls = [(script, args) for script, args in calls if script == "run_data_jobs.py"
                      and args[:2] == ["--job", "symbol_universe"]]
    assert len(universe_calls) == 1
    assert "--execute" in universe_calls[0][1]
    assert len(baseline_calls) == 1
    assert baseline_calls[0][1][baseline_calls[0][1].index("--date") + 1] == "2026-10-19"
    baseline_step = next(step for step in report["steps"] if step["step"] == "auction_baseline")
    assert baseline_step["trade_date"] == "2026-10-19"


def test_prep_does_not_mark_future_artifact_ready(tmp_path):
    catalog = ArtifactCatalog(tmp_path)
    catalog.commit("daily_kline", "2026-10-16", [{"symbol": "600001"}], {
        "trade_date": "2026-10-16", "row_count": 1,
        "available_at": "2026-10-20T00:00:00+08:00",
    }, source="fixture")
    report = build_prep_report(tmp_path, "2026-10-19", write=False,
                               asof="2026-10-19T08:00:00+08:00")
    step = next(step for step in report["steps"] if step["step"] == "previous_daily_kline")
    assert step["status"] == "MISSING"
    assert step["reason"] == "ARTIFACT_NOT_PIT_READY"

    catalog.commit("auction_baseline", "2026-10-19", [{"row_kind": "market_baseline"}], {
        "schema": "auction_baseline_v3", "trade_date": "2026-10-19", "window_days": 64,
        "available_at": "2026-10-20T00:00:00+08:00",
    }, source="fixture")
    from engine_core.auction_baseline import _existing
    assert _existing(catalog, "2026-10-19") is None
