import json
from pathlib import Path

def _seed_c325(root):
    fixture = Path(__file__).parent / "fixtures" / "trade_calendar"
    payload = json.loads((fixture / "baostock-2026-01-01_2026-12-31.json").read_bytes())
    dates = [row["calendar_date"] for row in payload["rows"] if row["is_trading_day"] == "1"]
    raw = {path.name: path.read_bytes() for path in fixture.glob("*.json")}
    from engine_core.data_pipeline.trade_calendar import TradeCalendarStore
    TradeCalendarStore(root).publish_confirmed(dates, source="C325 BaoStock+SZSE",
        fetched_at="2026-10-20T00:00:00+00:00", coverage_start="2026-01-01",
        coverage_end="2026-12-31", raw_responses=raw)

from engine_core.prep_trade_day import build_prep_report, previous_trade_date
from examples.core_guardian import HANDLER_IMPLEMENTATIONS
from examples.run_data_jobs import _plate_codes
from engine_core.data_pipeline.storage import ArtifactCatalog


# DATA: spec (prep report shape per prep_trade_day / core_guardian._prep_failure)
def test_prep_configured_step_error_degrades(tmp_path, monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace
    from examples import core_guardian

    report = {"status": "PARTIAL", "errors": ["plate_amount_baseline:TD_RESOURCE_EXHAUSTED"],
              "error_steps": [{"step": "plate_amount_baseline", "code": "TD_RESOURCE_EXHAUSTED"}],
              "steps": [{"step": "plate_amount_baseline", "status": "MISSING"}],
              "warnings": ["baseline unavailable"]}
    monkeypatch.setattr(core_guardian.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps(report), stderr=""))
    handler = core_guardian._command_handler("premarket_data", SimpleNamespace(write=True), tmp_path)
    event = SimpleNamespace(data_date_offset=0, timeout_seconds=10,
                            args={"prep_degradable_steps": ["plate_amount_baseline"]})
    result = handler(event, datetime.fromisoformat("2026-10-19T08:50:00+08:00"), {}, False)
    assert result.startswith("DEGRADED:prep_degraded_steps:plate_amount_baseline=TD_RESOURCE_EXHAUSTED")
    assert "baseline unavailable" in result
    report["errors"] = []
    report["error_steps"] = []
    assert handler(event, datetime.fromisoformat("2026-10-19T08:50:00+08:00"), {}, False).startswith(
        "DEGRADED:prep_degraded_steps:plate_amount_baseline=?")


# DATA: spec (prep report shape per prep_trade_day / core_guardian._prep_failure)
def test_prep_unconfigured_and_blocking_errors_still_fail(tmp_path, monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace
    import pytest
    from examples import core_guardian

    report = {"status": "PARTIAL", "errors": ["plate_amount_baseline:TD_RESOURCE_EXHAUSTED"],
              "error_steps": [{"step": "plate_amount_baseline", "code": "TD_RESOURCE_EXHAUSTED"}],
              "steps": [{"step": "plate_amount_baseline", "status": "MISSING"}]}
    monkeypatch.setattr(core_guardian.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps(report), stderr=""))
    handler = core_guardian._command_handler("premarket_data", SimpleNamespace(write=True), tmp_path)
    event = SimpleNamespace(data_date_offset=0, timeout_seconds=10, args={})
    now = datetime.fromisoformat("2026-10-19T08:50:00+08:00")
    with pytest.raises(Exception, match="plate_amount_baseline"):
        handler(event, now, {}, False)
    event.args = {"prep_degradable_steps": "plate_amount_baseline"}
    with pytest.raises(Exception, match="plate_amount_baseline"):
        handler(event, now, {}, False)
    report["errors"].append("theme_snapshot:COMMAND_FAILED")
    report["error_steps"].append({"step": "theme_snapshot", "code": "COMMAND_FAILED"})
    event.args = {"prep_degradable_steps": ["plate_amount_baseline"]}
    with pytest.raises(Exception) as exc:
        handler(event, now, {}, False)
    assert "theme_snapshot" in str(exc.value)
    assert "plate_amount_baseline" not in str(exc.value)
    report.pop("error_steps")
    report["error_codes"] = ["TD_RESOURCE_EXHAUSTED", "COMMAND_FAILED"]
    with pytest.raises(Exception) as exc:
        handler(event, now, {}, False)
    assert "theme_snapshot=COMMAND_FAILED" in str(exc.value)
    assert "plate_amount_baseline" not in str(exc.value)


def test_all_guardian_handlers_have_explicit_core_mapping_or_not_implemented():
    expected = {
        "night_report", "premarket_data", "kaipan_fetch", "auction_watch",
        "kaipan_hot_capture", "snapshot_check", "auction_report", "open_report",
        "intraday_data", "postmarket_data", "baostock_probe", "close_capture", "close_report",
        "calendar_refresh", "daily_kline_backfill",
    }
    assert set(HANDLER_IMPLEMENTATIONS) == expected
    assert all(value and value != "" for value in HANDLER_IMPLEMENTATIONS.values())
    assert "NOT_IMPLEMENTED" in HANDLER_IMPLEMENTATIONS.values()
    assert HANDLER_IMPLEMENTATIONS["premarket_data"] == "examples/prep_trade_day.py:premarket_and_auction_baseline"


def test_previous_trade_date_uses_calendar_and_never_guesses_from_supplied_dates(tmp_path):
    _seed_c325(tmp_path)
    assert previous_trade_date(tmp_path, "2026-10-19") == "2026-10-16"
    (tmp_path / "trade_dates.json").unlink()
    assert previous_trade_date(tmp_path, "2027-01-04", known_trade_dates=["2026-10-15", "2026-10-16"]) is None


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

    _seed_c325(tmp_path)
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
    _seed_c325(tmp_path)
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
    assert _existing(catalog, "2026-10-19", asof="2026-10-19T08:00:00+08:00") is None


def test_premarket_command_preserves_explicit_asof(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from engine_core.guardian.commands import build_handler_command
    event = SimpleNamespace(args={}, asof="2026-10-19T08:00:00+08:00", event_id="startup_0830")
    command = build_handler_command("premarket_data", event, SimpleNamespace(write=False),
                                    tmp_path, "2026-10-19", "python", tmp_path)
    assert command[command.index("--asof") + 1] == event.asof
    from examples import core_guardian, guardian_handler
    captured = []
    def fake_command_handler(name, opts, root):
        def run(event, now, context, degraded):
            captured.append(build_handler_command(name, event, opts, root, "2026-10-19", "python", tmp_path))
            return "ok"
        return run
    monkeypatch.setattr(core_guardian, "_command_handler", fake_command_handler)
    monkeypatch.setenv("GUARDIAN_DRY_RUN", "1")
    request = {"event": "startup_0830", "trade_date": "2026-10-19", "asof": event.asof}
    assert guardian_handler._run_event(request)[0] == 0
    assert captured[-1][captured[-1].index("--asof") + 1] == event.asof
    request.pop("asof")
    assert guardian_handler._run_event(request)[0] == 0
    assert "--asof" not in captured[-1]
    event.asof = None
    assert "--asof" not in build_handler_command("premarket_data", event, SimpleNamespace(write=False),
                                                  tmp_path, "2026-10-19", "python", tmp_path)


def test_prep_timings_and_progress_with_stubbed_jobs(tmp_path, monkeypatch):
    import engine_core.prep_trade_day as prep
    import redis
    _seed_c325(tmp_path)
    snapshots = []
    class Client:
        def set(self, key, value, ex):
            snapshots.append((key, json.loads(value), ex))
    monkeypatch.setenv("CORE_REDIS_URL", "redis://stub")
    monkeypatch.setattr(redis.Redis, "from_url", lambda *a, **k: Client())
    monkeypatch.setattr(prep, "_run", lambda *a: (0, '{"reused": false}'))
    monkeypatch.setattr(prep, "_prepare_theme_snapshot_cache", lambda *a, **k: True)
    report = prep.build_prep_report(tmp_path, "2026-10-19", write=True,
                                    asof="2026-10-19T08:00:00+08:00", event_id="startup_0830")
    records = {item["step"]: item for item in report["step_timings"]}
    assert {item["step"] for item in report["steps"]} <= records.keys()
    assert all({"step", "dataset", "trade_date", "status", "check_ms", "filled", "fill_script", "fill_exit", "fill_ms"} <= item.keys() for item in records.values())
    baseline = records["auction_baseline"]
    assert (baseline["filled"], baseline["fill_script"], baseline["fill_exit"], baseline["reused_prior_version"]) == (True, "build_auction_baseline.py", 0, False)
    assert any(p["phase"] == "filling" and p["current_step"] == "auction_baseline" for _, p, _ in snapshots)
    assert snapshots[-1][1]["step_timings"] == report["step_timings"]
    assert all(k == report["report_key"] and ttl == 14 * 86400 for k, _, ttl in snapshots)


def test_prep_redis_failure_does_not_change_result(tmp_path, monkeypatch):
    import redis
    class Broken:
        def set(self, *args, **kwargs):
            raise OSError("unavailable")
    monkeypatch.delenv("CORE_REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    expected = build_prep_report(tmp_path, "2026-10-19", asof="2026-10-19T08:00:00+08:00")
    monkeypatch.setenv("CORE_REDIS_URL", "redis://stub")
    monkeypatch.setattr(redis.Redis, "from_url", lambda *a, **k: Broken())
    actual = build_prep_report(tmp_path, "2026-10-19", asof="2026-10-19T08:00:00+08:00")
    for key in ("status", "steps", "errors", "warnings"):
        if key == "steps":
            assert [{k: v for k, v in row.items() if k != "elapsed_ms"} for row in actual[key]] == [{k: v for k, v in row.items() if k != "elapsed_ms"} for row in expected[key]]
        else:
            assert actual[key] == expected[key]
