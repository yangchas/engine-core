from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

from engine_core.data_pipeline.storage import ArtifactCatalog


_UNIVERSE_FIXTURE = (Path(__file__).parent / "fixtures" / "symbol_universe" /
                     "baostock_query_all_stock_20261016.json")


def _raw_universe():
    return json.loads(_UNIVERSE_FIXTURE.read_text(encoding="utf-8"))


def _normalized_universe(asof: str = "2026-10-16"):
    from examples.run_data_jobs import _normalize_universe_rows

    rows = _normalize_universe_rows(_raw_universe())
    return [dict(row, universe_asof=asof) for row in rows]


def _seed_calendar(root: Path):
    fixture_dir = Path(__file__).parent / "fixtures" / "trade_calendar"
    payload = json.loads((fixture_dir / "baostock-2026-01-01_2026-12-31.json").read_text())
    dates = [row["calendar_date"] for row in payload["rows"]
             if row["is_trading_day"] == "1"]
    from engine_core.data_pipeline.trade_calendar import TradeCalendarStore

    TradeCalendarStore(root).publish_confirmed(
        dates, source="fixture_real_baostock_szse", fetched_at="2026-10-20T00:00:00Z",
        coverage_start="2026-01-01", coverage_end="2026-12-31",
        raw_responses={path.name: path.read_bytes() for path in fixture_dir.glob("*.json")})


def _seed_universe(root: Path, day: str, *, asof: str = "2026-10-16"):
    rows = _normalized_universe(asof)
    manifest = ArtifactCatalog(root).commit(
        "symbol_universe", day, rows,
        {"trade_date": day, "as_of": asof, "universe_asof": asof,
         "quality": {"status": "READY", "coverage": 1.0}},
        source="baostock_query_all_stock")
    return rows, manifest


class _Result:
    error_code = "0"

    def __init__(self, rows):
        self.rows = rows
        self.index = -1

    def next(self):
        self.index += 1
        return self.index < len(self.rows)

    def get_row_data(self):
        return self.rows[self.index]


def _fake_baostock(rows=None, *, query_error=None, block_event=None):
    module = ModuleType("baostock")
    module.calls = []
    module.login = lambda: SimpleNamespace(error_code="0")
    module.logout = lambda: None

    def query_all_stock(day):
        module.calls.append(day)
        if block_event is not None:
            block_event.wait(30)
        if query_error is not None:
            raise query_error
        return _Result(rows or _raw_universe())

    module.query_all_stock = query_all_stock
    return module


def _run_universe_cli(root: Path, fake_bs, monkeypatch, capsys, *extra,
                      trade_date: str = "2026-10-19"):
    import sys
    from examples.run_data_jobs import main

    monkeypatch.setitem(sys.modules, "baostock", fake_bs)
    code = main(["--job", "symbol_universe", "--date", trade_date,
                 "--artifact-root", str(root), *extra])
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    return code, json.loads(lines[-1]), captured


def test_symbol_universe_primary_emits_contract_json(capsys, monkeypatch, tmp_path):
    _seed_calendar(tmp_path)
    _seed_universe(tmp_path, "2026-10-16")
    fake_bs = _fake_baostock()

    code, payload, captured = _run_universe_cli(
        tmp_path, fake_bs, monkeypatch, capsys, "--execute", "--baostock-timeout-seconds", "3")

    assert code == 0
    assert payload["status"] == "ok"
    assert payload["tier"] == 0
    assert payload["source"] == "baostock_query_all_stock"
    assert payload["row_count"] == len(_normalized_universe())
    assert captured.out.splitlines()[-1].startswith("{")
    assert fake_bs.calls == ["2026-10-16"]
    manifest_path = ArtifactCatalog(tmp_path).locate("symbol_universe", "2026-10-19")
    manifest = json.loads(manifest_path.read_text())
    assert manifest["tier"] == 0
    assert manifest["degraded"] is False


def test_symbol_universe_timeout_carries_forward_unchanged_rows(tmp_path, monkeypatch, capsys):
    _seed_calendar(tmp_path)
    source_rows, source_manifest = _seed_universe(tmp_path, "2026-10-23", asof="2026-10-22")
    fake_bs = _fake_baostock(query_error=TimeoutError("BAOSTOCK_TIMEOUT"))

    code, payload, _ = _run_universe_cli(
        tmp_path, fake_bs, monkeypatch, capsys, "--execute", "--baostock-timeout-seconds", "3",
        trade_date="2026-10-26")

    assert code == 10
    assert payload["status"] == "degraded"
    assert payload["tier"] == 1
    assert payload["source"] == "carried_forward"
    assert payload["error_code"] == "BAOSTOCK_TIMEOUT"
    target = ArtifactCatalog(tmp_path).locate("symbol_universe", "2026-10-26")
    manifest = json.loads(target.read_text())
    rows = ArtifactCatalog(tmp_path).read_rows(target)
    source_path = Path(source_manifest["manifest_uri"])
    source_rows_read = ArtifactCatalog(tmp_path).read_rows(source_path)
    assert manifest["carried_forward_from"] == "2026-10-23"
    assert manifest["universe_asof"] == "2026-10-22"
    assert manifest["tier"] == 1
    assert manifest["degraded"] is True
    assert manifest["quality"]["status"] == "DEGRADED"
    assert manifest["business_status"] != "FORMAL"
    assert rows == source_rows == source_rows_read
    assert "source=carried_forward" in manifest["storage_uri"]
    assert "source=baostock_query_all_stock" in source_manifest["storage_uri"]


def test_run_with_degrade_classifies_two_timeout_failures_as_retryable():
    from engine_core.data_pipeline.job_contract import run_with_degrade

    def timeout(_day):
        raise TimeoutError("BAOSTOCK_TIMEOUT")

    result, payload = run_with_degrade(
        "symbol_universe", "2026-10-26",
        [(0, "baostock_query_all_stock", timeout), (1, "carried_forward", timeout)],
        dataset="symbol_universe")

    assert payload is None
    assert result.status == "failed"
    assert result.retryable is True
    assert result.error_code == "BAOSTOCK_TIMEOUT"
    assert result.attempts == 2
    assert result.exit_code == 30


def test_run_with_degrade_classifies_two_quality_failures_as_nonretryable():
    from engine_core.data_pipeline.job_contract import run_with_degrade

    bad = lambda _day: {"rows": []}
    result, payload = run_with_degrade(
        "symbol_universe", "2026-10-26",
        [(0, "baostock_query_all_stock", bad), (1, "carried_forward", bad)],
        dataset="symbol_universe", quality_check=lambda value: bool(value["rows"]))

    assert payload is None
    assert result.status == "failed"
    assert result.retryable is False
    assert result.error_code == "QUALITY_INVALID"
    assert result.attempts == 2
    assert result.exit_code == 40


def test_baostock_timeout_argument_bounds_blocking_query_to_three_seconds(tmp_path, monkeypatch, capsys):
    _seed_calendar(tmp_path)
    release = threading.Event()
    fake_bs = _fake_baostock(block_event=release)
    started = time.monotonic()
    try:
        code, payload, _ = _run_universe_cli(
            tmp_path, fake_bs, monkeypatch, capsys,
            "--execute", "--baostock-timeout-seconds", "3", trade_date="2026-10-26")
    finally:
        release.set()
    elapsed = time.monotonic() - started

    assert code == 30
    assert payload["status"] == "failed"
    assert payload["error_code"] == "BAOSTOCK_TIMEOUT"
    assert 2.5 <= elapsed < 8.0


def test_contract_parse_handles_missing_mismatch_and_legacy_exit_75():
    from dataclasses import asdict
    from engine_core.data_pipeline.job_contract import JobResult, parse

    valid = JobResult(contract=1, job="symbol_universe", trade_date="2026-10-26",
                      status="ok", tier=0, source="baostock_query_all_stock",
                      dataset="symbol_universe", version=None, row_count=5,
                      coverage=1.0, error_code=None, retryable=False, attempts=1,
                      elapsed_ms=2.0, detail=None)
    good_text = json.dumps(asdict(valid))
    missing = parse("no json here", 0)
    mismatch = parse(good_text, 10)
    legacy = parse(good_text, 75)

    assert (missing.status, missing.retryable, missing.error_code) == (
        "failed", False, "CONTRACT_VIOLATION")
    assert (mismatch.status, mismatch.retryable, mismatch.error_code) == (
        "failed", False, "CONTRACT_VIOLATION")
    assert (legacy.status, legacy.retryable, legacy.error_code) == (
        "failed", True, "LEGACY_EXIT_75")


def test_prep_report_records_exit_10_as_partial_without_an_error(tmp_path, monkeypatch):
    import engine_core.prep_trade_day as prep
    from dataclasses import asdict
    from engine_core.data_pipeline.job_contract import JobResult

    _seed_calendar(tmp_path)
    _seed_universe(tmp_path, "2026-10-23", asof="2026-10-22")
    rows = _normalized_universe("2026-10-22")

    def fake_run(_repo, script, args):
        if args[:2] == ["--job", "symbol_universe"]:
            ArtifactCatalog(tmp_path).commit(
                "symbol_universe", "2026-10-26", rows,
                {"trade_date": "2026-10-26", "universe_asof": "2026-10-22",
                 "carried_forward_from": "2026-10-23", "tier": 1, "degraded": True,
                 "business_status": "PARTIAL", "quality": {"status": "DEGRADED"}},
                source="carried_forward")
            payload = JobResult(
                contract=1, job="symbol_universe", trade_date="2026-10-26",
                status="degraded", tier=1, source="carried_forward",
                dataset="symbol_universe", version="sha256-fixture", row_count=len(rows),
                coverage=1.0, error_code="BAOSTOCK_TIMEOUT", retryable=False,
                attempts=2, elapsed_ms=3000.0,
                detail={"carried_forward_from": "2026-10-23"})
            return 10, json.dumps(asdict(payload))
        return 0, ""

    monkeypatch.setattr(prep, "_run", fake_run)
    monkeypatch.setattr(prep, "_prepare_theme_snapshot_cache", lambda *_args, **_kwargs: True)
    report = prep.build_prep_report(
        tmp_path, "2026-10-26", write=True, asof="2026-10-26T08:00:00+08:00")

    step = next(item for item in report["steps"] if item["step"] == "symbol_universe")
    assert report["errors"] == []
    assert step["status"] == "PARTIAL"
    assert step["reason"] == "tier=1 carried_forward"


def test_repeated_carried_forward_run_is_idempotent_and_uses_distinct_source(tmp_path, monkeypatch, capsys):
    _seed_calendar(tmp_path)
    _, source_manifest = _seed_universe(tmp_path, "2026-10-23", asof="2026-10-22")
    fake_bs = _fake_baostock(query_error=TimeoutError("BAOSTOCK_TIMEOUT"))

    first_code, first, _ = _run_universe_cli(
        tmp_path, fake_bs, monkeypatch, capsys, "--execute", "--baostock-timeout-seconds", "3",
        trade_date="2026-10-26")
    second_code, second, _ = _run_universe_cli(
        tmp_path, fake_bs, monkeypatch, capsys, "--execute", "--baostock-timeout-seconds", "3",
        trade_date="2026-10-26")

    catalog = ArtifactCatalog(tmp_path)
    target = catalog.locate("symbol_universe", "2026-10-26")
    source = Path(source_manifest["manifest_uri"])
    target_manifest = json.loads(target.read_text())
    source_data = json.loads(source.read_text())
    versions = catalog.read()["datasets"]["symbol_universe"]["dates"]["2026-10-26"]
    assert first_code == second_code == 10
    assert first["version"] == second["version"] == target_manifest["version"]
    assert len(versions) == 1
    assert target_manifest["storage_uri"] != source_data["storage_uri"]
    assert "source=carried_forward" in target_manifest["storage_uri"]
    assert "source=baostock_query_all_stock" in source_data["storage_uri"]


def test_prep_retries_degraded_universe_and_upgrades_to_primary(tmp_path, monkeypatch):
    from dataclasses import asdict
    from engine_core.data_pipeline.access import get
    from engine_core.data_pipeline.job_contract import JobResult
    import engine_core.prep_trade_day as prep

    _seed_calendar(tmp_path)
    rows = _normalized_universe("2026-10-22")
    universe_calls = []

    def fake_run(_repo, script, args):
        if script == "run_data_jobs.py" and args[:2] == ["--job", "symbol_universe"]:
            universe_calls.append(list(args))
            degraded = len(universe_calls) == 1
            source = "carried_forward" if degraded else "baostock_query_all_stock"
            tier = 1 if degraded else 0
            manifest = ArtifactCatalog(tmp_path).commit(
                "symbol_universe", "2026-10-26", rows,
                {"trade_date": "2026-10-26", "universe_asof": "2026-10-22",
                 "carried_forward_from": "2026-10-23" if degraded else None,
                 "tier": tier, "degraded": degraded,
                 "business_status": "PARTIAL" if degraded else "FORMAL",
                 "quality": {"status": "DEGRADED" if degraded else "READY", "coverage": 1.0}},
                source=source)
            result = JobResult(
                contract=1, job="symbol_universe", trade_date="2026-10-26",
                status="degraded" if degraded else "ok", tier=tier, source=source,
                dataset="symbol_universe", version=manifest["version"], row_count=len(rows),
                coverage=1.0, error_code="BAOSTOCK_TIMEOUT" if degraded else None,
                retryable=False, attempts=2 if degraded else 1, elapsed_ms=1.0,
                detail={"carried_forward_from": "2026-10-23"} if degraded else None)
            return result.exit_code, json.dumps(asdict(result))
        return 0, ""

    monkeypatch.setattr(prep, "_run", fake_run)
    monkeypatch.setattr(prep, "_prepare_theme_snapshot_cache", lambda *_args, **_kwargs: True)
    first = prep.build_prep_report(
        tmp_path, "2026-10-26", write=True, asof="2026-10-26T12:00:00+08:00")
    second = prep.build_prep_report(
        tmp_path, "2026-10-26", write=True, asof="2026-10-26T12:00:00+08:00")

    first_step = next(item for item in first["steps"] if item["step"] == "symbol_universe")
    second_step = next(item for item in second["steps"] if item["step"] == "symbol_universe")
    catalog = ArtifactCatalog(tmp_path)
    located = json.loads(catalog.locate("symbol_universe", "2026-10-26").read_text())
    selected = get("symbol_universe", trade_date="2026-10-26", view="raw", root=tmp_path)
    versions = catalog.read()["datasets"]["symbol_universe"]["dates"]["2026-10-26"]

    assert len(universe_calls) == 2
    assert first_step["status"] == "PARTIAL"
    assert first_step["reason"] == "tier=1 carried_forward"
    assert second["errors"] == []
    assert second_step["status"] == "READY"
    assert second_step["reason"] is None
    assert len(versions) == 2
    assert located["tier"] == 0
    assert selected.meta["source"] == "baostock_query_all_stock"
    assert selected.meta["version"] == located["version"]


def test_prep_repeated_degradation_keeps_existing_degraded_version_without_error(tmp_path, monkeypatch):
    from dataclasses import asdict
    from engine_core.data_pipeline.job_contract import JobResult
    import engine_core.prep_trade_day as prep

    _seed_calendar(tmp_path)
    rows = _normalized_universe("2026-10-22")
    catalog = ArtifactCatalog(tmp_path)
    fallback = catalog.commit(
        "symbol_universe", "2026-10-26", rows,
        {"trade_date": "2026-10-26", "universe_asof": "2026-10-22",
         "carried_forward_from": "2026-10-23", "tier": 1, "degraded": True,
         "business_status": "PARTIAL", "quality": {"status": "DEGRADED", "coverage": 1.0}},
        source="carried_forward")
    calls = []

    def fake_run(_repo, script, args):
        if script == "run_data_jobs.py" and args[:2] == ["--job", "symbol_universe"]:
            calls.append(list(args))
            if len(calls) == 1:
                repeated = catalog.commit(
                    "symbol_universe", "2026-10-26", rows,
                    {"trade_date": "2026-10-26", "universe_asof": "2026-10-22",
                     "carried_forward_from": "2026-10-23", "tier": 1, "degraded": True,
                     "business_status": "PARTIAL",
                     "quality": {"status": "DEGRADED", "coverage": 1.0}},
                    source="carried_forward")
                result = JobResult(
                    contract=1, job="symbol_universe", trade_date="2026-10-26",
                    status="degraded", tier=1, source="carried_forward",
                    dataset="symbol_universe", version=repeated["version"], row_count=len(rows),
                    coverage=1.0, error_code="BAOSTOCK_TIMEOUT", retryable=False, attempts=2,
                    elapsed_ms=1.0, detail=None)
                return 10, json.dumps(asdict(result))
            result = JobResult(
                contract=1, job="symbol_universe", trade_date="2026-10-26",
                status="failed", tier=0, source="baostock_query_all_stock",
                dataset="symbol_universe", version=None, row_count=None, coverage=None,
                error_code="BAOSTOCK_TIMEOUT", retryable=True, attempts=1,
                elapsed_ms=1.0, detail=None)
            return 30, json.dumps(asdict(result))
        return 0, ""

    monkeypatch.setattr(prep, "_run", fake_run)
    monkeypatch.setattr(prep, "_prepare_theme_snapshot_cache", lambda *_args, **_kwargs: True)
    first_report = prep.build_prep_report(
        tmp_path, "2026-10-26", write=True, asof="2026-10-26T12:00:00+08:00")
    second_report = prep.build_prep_report(
        tmp_path, "2026-10-26", write=True, asof="2026-10-26T12:00:00+08:00")

    first_step = next(item for item in first_report["steps"] if item["step"] == "symbol_universe")
    second_step = next(item for item in second_report["steps"] if item["step"] == "symbol_universe")
    current = json.loads(catalog.locate("symbol_universe", "2026-10-26").read_text())
    versions = catalog.read()["datasets"]["symbol_universe"]["dates"]["2026-10-26"]
    assert len(calls) == 2
    assert first_report["errors"] == second_report["errors"] == []
    assert first_step["status"] == second_step["status"] == "PARTIAL"
    assert first_step["reason"] == second_step["reason"] == "tier=1 carried_forward"
    assert current["version"] == fallback["version"]
    assert current["tier"] == 1 and current["degraded"] is True
    assert len(versions) == 1


def test_prep_does_not_retry_existing_primary_universe(tmp_path, monkeypatch):
    import engine_core.prep_trade_day as prep

    _seed_calendar(tmp_path)
    _seed_universe(tmp_path, "2026-10-26", asof="2026-10-26")
    calls = []

    def fake_run(_repo, script, args):
        if script == "run_data_jobs.py" and args[:2] == ["--job", "symbol_universe"]:
            calls.append(list(args))
        return 0, ""

    monkeypatch.setattr(prep, "_run", fake_run)
    monkeypatch.setattr(prep, "_prepare_theme_snapshot_cache", lambda *_args, **_kwargs: True)
    report = prep.build_prep_report(
        tmp_path, "2026-10-26", write=True, asof="2026-10-26T12:00:00+08:00")

    step = next(item for item in report["steps"] if item["step"] == "symbol_universe")
    assert calls == []
    assert step["status"] == "READY"
