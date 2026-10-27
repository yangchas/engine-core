"""C566: 15:05 daily-kline close rung handler, catalog rank rules, and config-driven ladder shape."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "real"
REQUEST_DATE = "2026-10-26"
REFERENCE_DATE = "2026-10-23"


def _schedule():
    return json.loads((ROOT / "src/engine_core/guardian/schedule.json").read_text(encoding="utf-8"))


def _commit(root, day, provider, *, business="PROVISIONAL", degraded=True, sample=False, tier=None):
    from engine_core.data_pipeline.storage import ArtifactCatalog

    manifest = {"schema_version": 1, "dataset": "daily_kline", "trade_date": day, "provider": provider,
                "source": provider, "business_status": business, "degraded": degraded,
                "quality": {"status": "DEGRADED" if degraded else "FORMAL", "coverage": 1.0,
                            "field_completeness": {}},
                "available_at": day + "T15:05:00+08:00"}
    if tier is not None:
        manifest["tier"] = tier
    if sample:
        manifest["sample_scope"] = "symbols_file"
    rows = [{"symbol": "000001", "date": day, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
             "pre_close": 1.0, "pct_chg": 0, "volume": 1, "amount_yuan": 1.0}]
    if sample:
        return None
    return ArtifactCatalog(root).commit("daily_kline", day, rows, manifest, source=provider)


def _request(providers, day=REQUEST_DATE, timeout=600):
    return {"contract": 1, "event": "daily_kline_close_1505", "trade_date": day, "data_date": day,
            "args": {"providers": providers}, "timeout_seconds": timeout}


def _runner(returncode, contract, stderr=""):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        out = json.dumps(contract) + "\n" if contract is not None else ""
        return SimpleNamespace(returncode=returncode, stdout=out, stderr=stderr)
    run.calls = calls
    return run


def _contract(code="", retryable=False, status="failed", **extra):
    return {"contract": 1, "status": status, "error_code": code, "retryable": retryable, **extra}


def test_schedule_declares_the_1505_event_and_ladder_order():
    schedule = _schedule()
    event = next(e for e in schedule["events"] if e["event_id"] == "daily_kline_close_1505")
    probe = next(e for e in schedule["events"] if e["event_id"] == "baostock_probe_1730")
    profile = schedule["profiles"][event["profile"]]
    assert event["at"] == "15:05:00" and event["calendar"] == "trading_day"
    assert event["cost"] == "heavy" and probe["cost"] == "heavy"
    assert event["catchup_mode"] == "run_in_window" and profile["catchup"] == "run_in_window"
    assert event["late_until"] < probe["available_after"] and event["late_until"] == "17:35:00"
    assert [t["name"] for t in profile["ladder"]] == ["wencai", "q2_final"]
    assert [t["args"]["providers"] for t in profile["ladder"]] == ["wencai", "q2_final"]
    assert all(t["on_timeout"] == "advance" and t["alert_on_fallback"] for t in profile["ladder"])
    for other in schedule["events"]:
        if other["event_id"] == event["event_id"]:
            continue
        ladder = other.get("ladder") or schedule["profiles"].get(other.get("profile"), {}).get("ladder", [])
        assert not any(t.get("on_timeout") or t.get("alert_on_fallback") for t in ladder), other["event_id"]


def test_schedule_validator_rejects_bad_tier_flags():
    from engine_core.guardian.management import validate_schedule

    payload = copy.deepcopy(_schedule())
    payload["profiles"]["daily_kline_close"]["ladder"][0]["on_timeout"] = "ignore"
    with pytest.raises(ValueError, match="on_timeout"):
        validate_schedule(payload)
    payload = copy.deepcopy(_schedule())
    payload["profiles"]["daily_kline_close"]["ladder"][0]["alert_on_fallback"] = "yes"
    with pytest.raises(ValueError):
        validate_schedule(payload)
    assert validate_schedule(copy.deepcopy(_schedule()))


def test_missing_or_unknown_providers_are_config_errors_never_defaulted(tmp_path):
    from engine_core.guardian.daily_kline_close import run_close_tier

    run = _runner(0, _contract(status="ok"))
    for args in (None, {}, {"providers": ""}, {"providers": "baostock,nope"}):
        code, payload = run_close_tier({**_request("x"), "args": args}, tmp_path, runner=run)
        assert (code, payload["error_code"]) == (64, "CONFIG_ERROR")
    assert run.calls == []


def test_command_uses_only_the_rung_provider_and_timeout_budget(tmp_path):
    from engine_core.guardian.daily_kline_close import run_close_tier

    run = _runner(10, _contract(status="degraded", version="v1", source="wencai"))
    code, payload = run_close_tier(_request("wencai", timeout=600), tmp_path, runner=run)
    cmd, kwargs = run.calls[0]
    assert (code, payload["status"]) == (10, "degraded")
    assert cmd[cmd.index("--providers") + 1] == "wencai"
    assert cmd[cmd.index("--date") + 1] == REQUEST_DATE and "--write" in cmd
    assert cmd[cmd.index("--budget-seconds") + 1] == "540" and kwargs["timeout"] == 600.0
    code, _ = run_close_tier(_request("q2_final"), tmp_path, runner=run)
    assert run.calls[1][0][run.calls[1][0].index("--providers") + 1] == "q2_final"


@pytest.mark.parametrize("rc,contract,expect", [
    (0, _contract(status="ok"), (0, "")),
    (30, _contract("TIER_PARTIAL", True), (30, "TIER_PARTIAL")),
    (40, _contract("TIER_NO_DATA"), (40, "TIER_NO_DATA")),
    (40, _contract("TIER_DATE_MISMATCH"), (40, "TIER_DATE_MISMATCH")),
    (40, _contract("DAILY_KLINE_ALL_PROVIDERS_FAILED"), (40, "DETERMINISTIC_FAILURE")),
    (2, None, (40, "DETERMINISTIC_FAILURE")),
])
def test_exit_mapping_keeps_tier_verdicts_and_collapses_the_rest(tmp_path, rc, contract, expect):
    from engine_core.guardian.daily_kline_close import run_close_tier

    code, payload = run_close_tier(_request("wencai"), tmp_path, runner=_runner(rc, contract))
    assert (code, payload["error_code"]) == expect


def test_subprocess_timeout_is_a_retryable_timeout(tmp_path):
    from engine_core.guardian.daily_kline_close import run_close_tier

    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
    code, payload = run_close_tier(_request("wencai"), tmp_path, runner=run)
    assert (code, payload["error_code"], payload["retryable"]) == (30, "FETCH_TIMEOUT", True)


def test_dry_run_never_runs_the_fetch(tmp_path):
    from engine_core.guardian.daily_kline_close import run_close_tier

    run = _runner(0, _contract(status="ok"))
    code, payload = run_close_tier(_request("wencai"), tmp_path, dry_run=True, runner=run)
    assert (code, payload["status"]) == (0, "not_applicable") and run.calls == []


@pytest.mark.parametrize("existing,providers,satisfied", [
    (None, "wencai", False),
    ("formal", "wencai", True),            # Baostock FORMAL already there: never lower the partition
    ("formal", "q2_final", True),
    ("wencai", "wencai", True),            # same level: no repeat
    ("wencai", "q2_final", True),
    ("q2_final", "wencai", False),         # a better tier may still replace a worse one
    ("q2_final", "q2_final", True),
])
def test_already_satisfied_follows_catalog_rank(tmp_path, existing, providers, satisfied):
    from engine_core.guardian.daily_kline_close import run_close_tier

    if existing == "formal":
        _commit(tmp_path, REQUEST_DATE, "baostock", business="FORMAL", degraded=False)
    elif existing in {"wencai", "q2_final"}:
        _commit(tmp_path, REQUEST_DATE, existing)
    run = _runner(10, _contract(status="degraded"))
    code, payload = run_close_tier(_request(providers), tmp_path, runner=run)
    if satisfied:
        assert (code, payload["error_code"]) == (20, "ALREADY_SATISFIED") and run.calls == []
    else:
        assert code == 10 and len(run.calls) == 1


def test_sample_manifests_and_unknown_sources_rank_correctly():
    from engine_core.guardian.daily_kline_close import _rank

    assert _rank({"sample_scope": "symbols_file", "provider": "baostock"}) is None
    assert _rank({"provider": "baostock"}) == 0 and _rank({"source": "legacy_q2_final"}) == 2
    assert _rank({"business_status": "FORMAL", "source": "baostock>wencai>q2_final"}) == 0
    assert _rank({"business_status": "PROVISIONAL", "source": "mystery"}) == 3


def test_other_days_do_not_satisfy(tmp_path):
    from engine_core.guardian.daily_kline_close import run_close_tier

    _commit(tmp_path, "2026-10-23", "baostock", business="FORMAL", degraded=False)
    code, _ = run_close_tier(_request("wencai"), tmp_path, runner=_runner(10, _contract(status="degraded")))
    assert code == 10


def test_guardian_handler_dispatches_to_the_rung(monkeypatch, tmp_path):
    from examples import guardian_handler

    monkeypatch.setenv("CORE_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("GUARDIAN_DRY_RUN", raising=False)
    seen = []
    import engine_core.guardian.daily_kline_close as module
    monkeypatch.setattr(module, "run_close_tier",
                        lambda request, root, dry_run=False: seen.append((request["args"], str(root), dry_run))
                        or (0, {"contract": 1, "status": "ok"}))
    code, _ = guardian_handler._run({**_request("q2_final"), "handler": "daily_kline_close"})
    assert code == 0 and seen == [({"providers": "q2_final"}, str(tmp_path), False)]


# --- end to end through the real chain and catalog, with captured WenCai rows (no network) ---------------

def _captured():
    from engine_core.wencai_fallback import _symbol

    captured = json.loads((FIXTURES / "wencai_daily_kline_2026-10-26_2109.json").read_text(encoding="utf-8"))
    return captured["rows"], _symbol


def _seed(tmp_path, rows, symbol_of):
    import gzip
    from conftest import read_real_artifact
    from engine_core.data_pipeline.storage import ArtifactCatalog

    catalog = ArtifactCatalog(tmp_path)
    manifest, universe = read_real_artifact("symbol_universe", REFERENCE_DATE, "actual")
    wanted = {symbol_of(row) for row in rows}
    universe = [row for row in universe if str(row.get("symbol", "")).zfill(6) in wanted]
    catalog.commit("symbol_universe", REQUEST_DATE, universe, manifest,
                   source=manifest.get("source", "baostock_query_all_stock"))
    ref_manifest = json.loads((FIXTURES / "core_artifacts/c522/daily_kline" / REFERENCE_DATE / "formal/manifest.json")
                              .read_text(encoding="utf-8"))
    with gzip.open(FIXTURES / "core_artifacts/c522/daily_kline" / REFERENCE_DATE / "formal/rows.jsonl.gz", "rt",
                   encoding="utf-8") as handle:
        reference = [json.loads(line) for line in handle if line.strip()]
    reference = [row for row in reference if row.get("symbol") in wanted]
    catalog.commit("daily_kline", REFERENCE_DATE, reference, ref_manifest, source=ref_manifest.get("source", "baostock"))
    return catalog


def _inprocess_runner(monkeypatch, rows, extra=()):
    from examples import run_data_jobs
    from engine_core.data_pipeline.providers.wencai_daily_adapter import fetch_daily_kline

    def fetch(day, _context=None):
        return fetch_daily_kline(day, query_fn=lambda **_kw: {"data": rows, "row_count": len(rows)},
                                 page_size=max(1, len(rows)), max_pages=1)
    monkeypatch.setattr(run_data_jobs, "_default_adapters", lambda *_a, **_k: {"wencai_daily_kline": fetch})

    def run(cmd, **_kwargs):
        argv = list(cmd[2:]) + ["--wencai-reference-strategy", "explicit", "--wencai-reference-date",
                                REFERENCE_DATE, "--wencai-min-interval-seconds", "0", *extra]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run_data_jobs.main(argv)
        return SimpleNamespace(returncode=code, stdout=buffer.getvalue(), stderr="")
    return run


def test_e2e_wencai_rung_commits_provisional_tier_version_and_a_rerun_is_satisfied(tmp_path, monkeypatch):
    from engine_core.guardian.daily_kline_close import run_close_tier

    rows, symbol_of = _captured()
    catalog = _seed(tmp_path, rows, symbol_of)
    run = _inprocess_runner(monkeypatch, rows)
    code, payload = run_close_tier(_request("wencai"), tmp_path, runner=run)
    assert (code, payload["status"]) == (10, "degraded"), payload
    locate = catalog.locate("daily_kline", REQUEST_DATE)
    manifest = json.loads(locate.read_text(encoding="utf-8"))
    assert manifest["provider"] == "wencai" and manifest["tier"] == 1
    assert manifest["business_status"] == "PROVISIONAL" and manifest["degraded"] is True
    assert manifest["status"] == "DEGRADED:wencai"
    again, payload = run_close_tier(_request("wencai"), tmp_path, runner=run)
    assert (again, payload["error_code"]) == (20, "ALREADY_SATISFIED")
    # a lower-quality rung never runs once a better-or-equal version exists
    lower, payload = run_close_tier(_request("q2_final"), tmp_path, runner=run)
    assert (lower, payload["error_code"]) == (20, "ALREADY_SATISFIED")


def test_e2e_wrong_column_date_is_a_tier_verdict_and_leaves_the_partition_clean(tmp_path, monkeypatch):
    from engine_core.guardian.daily_kline_close import run_close_tier

    rows, symbol_of = _captured()
    wrong = [{key.replace("[20261026]", "[20261025]"): value for key, value in row.items()} for row in rows]
    catalog = _seed(tmp_path, rows, symbol_of)
    run = _inprocess_runner(monkeypatch, wrong)
    code, payload = run_close_tier(_request("wencai"), tmp_path, runner=run)
    assert (code, payload["error_code"], payload["retryable"]) == (40, "TIER_DATE_MISMATCH", False)
    assert catalog.locate("daily_kline", REQUEST_DATE) is None


def test_e2e_empty_wencai_answer_is_tier_no_data(tmp_path, monkeypatch):
    from engine_core.guardian.daily_kline_close import run_close_tier

    rows, symbol_of = _captured()
    catalog = _seed(tmp_path, rows, symbol_of)
    run = _inprocess_runner(monkeypatch, [])
    code, payload = run_close_tier(_request("wencai"), tmp_path, runner=run)
    assert (code, payload["error_code"]) == (40, "TIER_NO_DATA"), payload
    assert catalog.locate("daily_kline", REQUEST_DATE) is None


def _calendar(root, days):
    path = root / "trade_calendar"
    path.mkdir(parents=True, exist_ok=True)
    (path / "calendar.json").write_text(json.dumps({"days": [{"d": d, "t": 1} for d in days]}), encoding="utf-8")


@pytest.mark.parametrize("trigger,now,includes_today", [
    ("17:40:00", "2026-10-27T17:39:59+08:00", False),
    ("17:40:00", "2026-10-27T17:40:00+08:00", True),
    ("18:30", "2026-10-27T17:50:00+08:00", False),    # moving the event moves the boundary: no baked-in 17:30
    ("16:00:00", "2026-10-27T16:00:01+08:00", True),
])
def test_daily_kline_candidates_follow_the_registry_trigger_event(tmp_path, monkeypatch, trigger, now, includes_today):
    from datetime import datetime
    from engine_core.guardian import scheduler, startup

    _calendar(tmp_path, ["2026-10-26", "2026-10-27"])
    monkeypatch.setattr(startup, "load_registry", lambda: [
        {"id": "daily_kline", "late_policy": {"trigger_event": "the_trigger"}}])
    monkeypatch.setattr(scheduler, "_schedule_source",
                        lambda path=None: {"events": [{"event_id": "the_trigger", "at": trigger}]})
    days = startup._daily_kline_candidates(tmp_path, "2026-10-27", datetime.fromisoformat(now))
    assert ("2026-10-27" in days) is includes_today and "2026-10-26" in days


def test_daily_kline_candidates_refuse_an_unresolvable_trigger_event(tmp_path, monkeypatch):
    from datetime import datetime
    from engine_core.guardian import scheduler, startup

    _calendar(tmp_path, ["2026-10-27"])
    monkeypatch.setattr(startup, "load_registry", lambda: [
        {"id": "daily_kline", "late_policy": {"trigger_event": "gone"}}])
    monkeypatch.setattr(scheduler, "_schedule_source", lambda path=None: {"events": []})
    with pytest.raises(ValueError, match="trigger_event_unresolved"):
        startup._daily_kline_candidates(tmp_path, "2026-10-27", datetime.fromisoformat("2026-10-27T18:00:00+08:00"))


def test_fallback_alert_body_names_failed_and_succeeded_tiers():
    from examples import guardian_handler

    request = {"contract": 1, "event": "alert", "reason": "TIER_FALLBACK",
               "request": {"event": "daily_kline_close_1505", "date": "2026-10-27",
                           "error": "wencai:DETERMINISTIC_FAILURE -> q2_final"}}
    receipt = guardian_handler._send_alert(request, True)
    assert "daily_kline_close_1505: TIER_FALLBACK (1) wencai:DETERMINISTIC_FAILURE -> q2_final" in receipt["body"]
