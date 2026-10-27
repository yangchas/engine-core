"""Date and completeness checks replay captured WenCai upstream rows."""
from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path

import pytest


FIXTURES = Path(__file__).parent / "fixtures" / "real"
REQUEST_DATE = "2026-10-26"
REFERENCE_DATE = "2026-10-23"


def _captured_rows() -> tuple[list[dict], list[dict]]:
    from engine_core.wencai_fallback import _symbol

    captured = json.loads((FIXTURES / "wencai_daily_kline_2026-10-26_2109.json")
                          .read_text(encoding="utf-8"))
    daily_path = (FIXTURES / "core_artifacts" / "c522" / "daily_kline" /
                  REFERENCE_DATE / "formal" / "rows.jsonl.gz")
    with gzip.open(daily_path, "rt", encoding="utf-8") as handle:
        reference_rows = [json.loads(line) for line in handle if line.strip()]
    reference_symbols = {row["symbol"] for row in reference_rows}
    rows = [row for row in captured["rows"] if _symbol(row) in reference_symbols]
    return rows, reference_rows


def _payload(rows: list[dict]) -> dict:
    from engine_core.data_pipeline.providers.wencai_daily_adapter import fetch_daily_kline

    return fetch_daily_kline(
        REQUEST_DATE,
        query_fn=lambda **_kwargs: {"data": rows, "row_count": len(rows)},
        page_size=max(1, len(rows)),
        max_pages=1,
    )


def _chain(tmp_path: Path, rows: list[dict], *, include_reference: bool = True,
           expected_count: int | None = None, retry_attempts: int = 2,
           threshold: float = 0.95):
    from engine_core.data_pipeline.providers.daily_kline_chain import (
        DailyKlineProviderChain, WencaiDailyProvider,
    )
    from engine_core.data_pipeline.storage import ArtifactCatalog

    payload = _payload(rows)
    expected_count = expected_count or len(rows)
    catalog = ArtifactCatalog(tmp_path)
    if include_reference:
        from engine_core.wencai_fallback import _symbol

        manifest_path = (FIXTURES / "core_artifacts" / "c522" / "daily_kline" /
                         REFERENCE_DATE / "formal" / "manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        reference_rows = _captured_rows()[1]
        symbols = {_symbol(row) for row in rows}
        reference_rows = [row for row in reference_rows if row.get("symbol") in symbols]
        catalog.commit("daily_kline", REFERENCE_DATE, reference_rows, manifest,
                       source=manifest.get("source", "baostock"))

    provider = WencaiDailyProvider(
        lambda _day: payload,
        state_path=None,
        expected_count=expected_count,
        sleep_fn=lambda _seconds: None,
    )
    return DailyKlineProviderChain(
        providers={"wencai": provider},
        artifact_root=tmp_path,
        wencai_date_validation_threshold=threshold,
        wencai_reference_coverage_threshold=0.95,
        wencai_reference_strategy="explicit",
        wencai_reference_date=REFERENCE_DATE,
        wencai_retry_attempts=retry_attempts,
        wencai_retry_backoff_seconds=0,
        sleep_fn=lambda _seconds: None,
    )


def test_partial_timeout_precedes_date_mismatch_and_is_checkpointed(tmp_path):
    from engine_core.data_pipeline.storage import ArtifactCatalog

    rows, _ = _captured_rows()
    changed = copy.deepcopy(rows[:1])
    changed[0] = {key.replace("[20261026]", "[20261025]"): value
                  for key, value in changed[0].items()}
    payload = _payload(changed)
    payload.update(partial=True, error_code="UPSTREAM_TIMEOUT")
    calls = []

    from engine_core.data_pipeline.providers.daily_kline_chain import (
        DailyKlineProviderChain, WencaiDailyProvider,
    )
    provider = WencaiDailyProvider(
        lambda _day: calls.append(_day) or payload,
        state_path=None,
        expected_count=len(rows),
        sleep_fn=lambda _seconds: None,
    )
    chain = DailyKlineProviderChain(
        providers={"wencai": provider}, artifact_root=tmp_path,
        wencai_reference_strategy="explicit", wencai_reference_date=REFERENCE_DATE,
        wencai_retry_attempts=2, wencai_retry_backoff_seconds=0,
        sleep_fn=lambda _seconds: None,
    )

    assert chain.fetch_chain(REQUEST_DATE, now="2026-10-26T16:00:00+08:00") is None
    assert chain.last_attempts
    assert all(item["error_code"] == "UPSTREAM_TIMEOUT" for item in chain.last_attempts)
    assert all(item["retryable"] is True for item in chain.last_attempts)
    assert not any(item["error_code"] == "TIER_DATE_MISMATCH" for item in chain.last_attempts)
    assert calls == [REQUEST_DATE, REQUEST_DATE]
    assert (tmp_path / ".checkpoints" / "daily_kline-wencai-2026-10-26.json").exists()
    assert ArtifactCatalog(tmp_path).locate("daily_kline", REQUEST_DATE) is None


def test_cli_returns_retryable_wencai_partial_code_30(tmp_path, monkeypatch, capsys):
    from conftest import seed_real_symbol_universe
    from examples import run_data_jobs
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from engine_core.data_pipeline.providers.wencai_daily_adapter import fetch_daily_kline

    universe = seed_real_symbol_universe(ArtifactCatalog(tmp_path), REFERENCE_DATE)
    active_symbols = {row["symbol"] for row in universe if str(row.get("trade_status")) != "0"}
    from engine_core.wencai_fallback import _symbol
    raw_row = next(row for row in _captured_rows()[0] if _symbol(row) in active_symbols)

    def fetch_partial(day, _context=None):
        payload = fetch_daily_kline(
            day,
            query_fn=lambda **_kwargs: {"data": [raw_row], "row_count": 1},
            page_size=1,
            max_pages=1,
        )
        payload.update(partial=True, error_code="TIER_PARTIAL")
        return payload

    monkeypatch.setattr(run_data_jobs, "_default_adapters",
                        lambda *_args, **_kwargs: {"wencai_daily_kline": fetch_partial})
    exit_code = run_data_jobs.main([
        "--job", "daily_kline", "--date", REFERENCE_DATE, "--execute",
        "--artifact-root", str(tmp_path), "--providers", "wencai",
        "--wencai-retry-attempts", "1",
    ])
    import json
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    contract = json.loads(lines[-1])

    assert exit_code == 30
    assert contract["error_code"] == "TIER_PARTIAL"
    assert contract["retryable"] is True
    assert contract["detail"]["provider_attempts"][-1]["coverage"] < 0.95
    assert ArtifactCatalog(tmp_path).locate("daily_kline", REFERENCE_DATE) is None


def test_complete_rows_with_wrong_column_date_are_permanently_rejected(tmp_path):
    from engine_core.data_pipeline.storage import ArtifactCatalog

    rows, _ = _captured_rows()
    changed = [{key.replace("[20261026]", "[20261025]"): value for key, value in row.items()}
               for row in rows]
    chain = _chain(tmp_path, changed)

    assert chain.fetch_chain(REQUEST_DATE, now="2026-10-27T16:00:00+08:00") is None
    assert chain.last_attempts[-1]["error_code"] == "TIER_DATE_MISMATCH"
    assert chain.last_attempts[-1]["retryable"] is False
    assert ArtifactCatalog(tmp_path).locate("daily_kline", REQUEST_DATE) is None


def test_complete_rows_with_matching_column_and_reference_evidence_pass(tmp_path):
    rows, _ = _captured_rows()
    chain = _chain(tmp_path, rows)

    result = chain.fetch_chain(REQUEST_DATE, now="2026-10-27T16:00:00+08:00")

    assert result is not None
    assert result.manifest["source"] == "wencai"
    assert result.manifest["wencai_date_validation"]["source_dates_status"] == "MATCH"
    assert result.manifest["wencai_date_validation"]["pre_close_match_ratio"] >= 0.95


def test_matching_column_date_with_conflicting_preclose_is_rejected(tmp_path):
    rows, _ = _captured_rows()
    changed = copy.deepcopy(rows)
    preclose = "前收盘价:不复权[20261026]"
    for row in changed[:2]:
        row[preclose] = float(row[preclose]) + 1.0
    chain = _chain(tmp_path, changed)

    assert chain.fetch_chain(REQUEST_DATE, now="2026-10-27T16:00:00+08:00") is None
    attempt = chain.last_attempts[-1]
    assert attempt["error_code"] == "TIER_DATE_MISMATCH"
    assert attempt["error_detail"]["source_dates_status"] == "MATCH"
    assert attempt["error_detail"]["reason"] == "column_date_and_value_evidence_conflict"
    assert attempt["error_detail"]["pre_close_match_ratio"] < 0.95


def test_date_validation_requires_reference_and_uses_configured_ratio_boundaries(tmp_path):
    from engine_core.data_pipeline.providers.daily_kline_chain import _ratio_meets_threshold

    assert _ratio_meets_threshold(94, 100, 0.94)
    assert not _ratio_meets_threshold(93, 100, 0.94)
    assert _ratio_meets_threshold(96, 100, 0.96)
    assert not _ratio_meets_threshold(95, 100, 0.96)

    rows, _ = _captured_rows()
    chain = _chain(tmp_path, rows, include_reference=False)
    assert chain.fetch_chain(REQUEST_DATE, now="2026-10-27T16:00:00+08:00") is None
    assert chain.last_attempts[-1]["error_code"] == "TIER_DATE_NO_REFERENCE"
    assert chain.last_attempts[-1]["retryable"] is False


def test_same_day_wencai_version_is_temporary_and_does_not_advance_latest(tmp_path):
    from engine_core.data_pipeline.storage import ArtifactCatalog

    rows, _ = _captured_rows()
    chain = _chain(tmp_path, rows)

    catalog_obj = ArtifactCatalog(tmp_path)
    previous = catalog_obj.locate("daily_kline", REFERENCE_DATE)
    assert previous is not None
    previous_manifest = json.loads(previous.read_text(encoding="utf-8"))
    assert catalog_obj.advance_daily_kline_latest(REFERENCE_DATE, previous_manifest)
    catalog = catalog_obj.read()["datasets"]["daily_kline"]
    previous_latest = dict(catalog["latest"])
    result = chain.fetch_chain(REQUEST_DATE, now="2026-10-26T16:00:00+08:00", persist=True)

    assert result is not None
    assert result.manifest["status"] == "SAME_DAY_TEMPORARY"
    assert result.manifest["business_status"] == "PROVISIONAL"
    catalog = catalog_obj.read()["datasets"]["daily_kline"]
    assert catalog["latest"] == previous_latest
    assert REQUEST_DATE not in (tmp_path / "daily_kline" / "latest.json").read_text()
