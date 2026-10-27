from __future__ import annotations

import gzip
import json
from pathlib import Path

from engine_core.data_pipeline.registry.daily_kline import classify_daily_kline
from engine_core.data_pipeline.storage import ArtifactCatalog


REAL = Path(__file__).parent / "fixtures" / "real" / "core_artifacts"
FIELDS = ("open", "high", "low", "close", "pre_close", "pct_chg", "amount_yuan", "volume")


def _artifact(name: str) -> tuple[dict, list[dict]]:
    base = REAL / name
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    with gzip.open(base / "rows.jsonl.gz", "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return manifest, rows


def _install_universe(catalog: ArtifactCatalog, day: str) -> None:
    _, rows = _artifact(f"c540/symbol_universe/{day}/actual")
    catalog.commit("symbol_universe", day, rows,
                   {"dataset": "symbol_universe", "trade_date": day,
                    "source": "baostock_query_all_stock", "business_status": "FORMAL"},
                   source="baostock_query_all_stock")


def test_classify_daily_kline_quality_branches():
    complete = {field: "COMPLETE" for field in FIELDS}
    assert classify_daily_kline("baostock", 1.0, complete) == "FORMAL"
    assert classify_daily_kline("baostock", 0.34, complete) == "PARTIAL"
    assert classify_daily_kline("wencai", 1.0, complete) == "PROVISIONAL"
    assert classify_daily_kline("q2_final", 1.0, complete) == "PROVISIONAL"
    incomplete = {**complete, "amount_yuan": "PARTIAL"}
    assert classify_daily_kline("baostock", 1.0, incomplete) == "PROVISIONAL"
    assert classify_daily_kline("baostock", None, complete) == "PROVISIONAL"
    assert classify_daily_kline("baostock", float("nan"), complete) == "PROVISIONAL"


def test_commit_uses_active_universe_denominator_for_real_artifacts(tmp_path):
    catalog = ArtifactCatalog(tmp_path)
    for day in ("2026-10-21", "2026-10-23"):
        _install_universe(catalog, day)

    baostock_manifest, baostock_rows = _artifact("c522/daily_kline/2026-10-23/formal")
    attempts = [{"provider": "baostock", "result": "ACCEPTED",
                 "attempted_at": baostock_manifest["fetched_at"], "elapsed_ms": 7906.136,
                 "reason": "coverage_accepted"}]
    result = catalog.commit("daily_kline", "2026-10-23", baostock_rows,
                            {**baostock_manifest, "provider": "baostock", "provider_attempts": attempts},
                            source="baostock")
    assert result["quality"]["expected_basis"] == "universe_active"
    assert result["quality"]["suspended_in_universe"] == 7
    assert result["quality"]["suspended_without_bar"] == 1
    assert result["quality"]["coverage"] == 1.0

    _, sparse_rows = _artifact("c540/daily_kline/2026-10-21/partial")
    sparse = catalog.commit("daily_kline", "2026-10-21", sparse_rows,
                            {"dataset": "daily_kline", "trade_date": "2026-10-21",
                             "provider": "UNKNOWN", "provider_attempts": [],
                             "field_completeness": {field: "COMPLETE" for field in FIELDS}},
                            source="baostock>wencai>q2_final")
    assert sparse["quality"]["coverage"] < 0.98
    assert sparse["quality"]["expected_basis"] == "universe_active"
    assert sparse["quality"]["suspended_without_bar"] == 3
    assert sparse["business_status"] == "PARTIAL"


def test_daily_kline_commit_normalizes_legacy_timestamps_and_compares_instants(tmp_path):
    from datetime import datetime, timezone

    from engine_core.data_pipeline.access import _datetime

    later_z = "2026-10-23T11:30:01Z"
    earlier_shanghai = "2026-10-23T19:30:00+08:00"
    assert _datetime(earlier_shanghai) < _datetime(later_z)

    _, rows = _artifact("c522/daily_kline/2026-10-23/formal")
    catalog = ArtifactCatalog(tmp_path)
    _install_universe(catalog, "2026-10-23")
    manifest = catalog.commit(
        "daily_kline", "2026-10-23", rows,
        {"dataset": "daily_kline", "trade_date": "2026-10-23", "source": "baostock",
         "provider": "baostock", "provider_attempts": [{"provider": "baostock", "result": "ACCEPTED",
            "attempted_at": "2026-10-23T11:30:00Z", "elapsed_ms": 1.0, "reason": "fixture"}],
         "fetched_at": "2026-10-23T11:30:00Z", "as_of": "2026-10-23T11:30:00Z",
         "field_completeness": {field: "COMPLETE" for field in FIELDS}},
        source="baostock", clock=lambda: datetime(2026, 10, 23, 11, 30, tzinfo=timezone.utc))
    assert manifest["available_at"].endswith("+08:00")
    assert manifest["fetched_at"] == "2026-10-23T19:30:00+08:00"
    assert manifest["as_of"] == "2026-10-23T19:30:00+08:00"
    assert manifest["provider_attempts"][0]["attempted_at"] == "2026-10-23T19:30:00+08:00"


def test_chain_replays_real_rows_and_appends_baostock_job_log(tmp_path):
    import gzip
    from datetime import datetime
    from engine_core.data_pipeline.providers.daily_kline_chain import DailyKlineProviderChain

    real_manifest, rows = _artifact("c522/daily_kline/2026-10-23/formal")

    class ReplayBaostock:
        def fetch(self, day, *, now=None):
            return {"rows": rows, "expected_count": len(rows), "source": "baostock",
                    "baostock_timing": {"login_ms": 2.0, "query_ms": 5.0,
                                         "result": "ok", "error_code": "0"}}

    chain = DailyKlineProviderChain(providers={"baostock": ReplayBaostock()}, artifact_root=tmp_path)
    _install_universe(chain.catalog, "2026-10-23")
    before = (tmp_path / "job_log" / "2026-10-23.jsonl")
    first = chain.fetch_chain("2026-10-23", now="2026-10-23T18:00:00+08:00", persist=True)
    assert first.manifest["provider"] == "baostock"
    attempt = first.manifest["provider_attempts"][0]
    assert {"provider", "result", "attempted_at", "elapsed_ms", "reason"} <= set(attempt)
    assert attempt["provider"] == "baostock" and attempt["result"] == "ok"
    assert attempt["chain_result"] == "ACCEPTED"
    assert first.manifest["business_status"] in {"FORMAL", "PROVISIONAL"}
    first_line = before.read_text(encoding="utf-8")
    chain.fetch_chain("2026-10-23", now="2026-10-23T18:00:00+08:00", persist=True)
    lines = before.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and lines[0] == first_line.strip()
    for line in lines:
        record = json.loads(line)
        assert record["provider"] == "baostock" and record["result"] == "ok"
        assert {"login_ms", "query_ms", "error_code", "started_at", "trade_date", "job"} <= set(record)

    from engine_core.daily_kline_degraded import capture

    raw_q2 = json.loads(gzip.open(REAL.parent / "c526/redis_q2_hashes_20261023_first60.json.gz", "rt",
                                  encoding="utf-8").read())

    class ReplayPipeline:
        def __init__(self):
            self.keys = []

        def hgetall(self, key):
            self.keys.append(key.rsplit(":", 1)[-1])

        def execute(self):
            return [raw_q2[symbol]["value"] for symbol in self.keys]

    class ReplayRedis:
        def smembers(self, key):
            return list(raw_q2)

        def pipeline(self, transaction=False):
            return ReplayPipeline()

    degraded = capture("2026-10-23", artifact_root=tmp_path, redis_client=ReplayRedis(),
                       now=datetime.fromisoformat("2026-10-23T18:00:00+08:00"),
                       write=True)
    assert degraded["manifest"]["provider"] == "q2_final"
    assert degraded["manifest"]["source"] == "q2_final"
    assert degraded["manifest"]["provider_chain"] == "q2_final>wencai"
    assert degraded["manifest"]["business_status"] != "FORMAL"
    degraded_attempt = degraded["manifest"]["provider_attempts"][0]
    assert {"provider", "result", "attempted_at", "elapsed_ms", "reason"} <= set(degraded_attempt)
    assert degraded_attempt["provider"] == "q2_final"


def test_job_runner_keeps_actual_provider_and_attempt_metadata(tmp_path):
    from engine_core.data_pipeline.jobs import JobRunner, RequestBudget
    from conftest import seed_real_symbol_universe

    day = "2026-10-23"
    _, rows = _artifact("c522/daily_kline/2026-10-23/formal")
    catalog = ArtifactCatalog(tmp_path)
    seed_real_symbol_universe(catalog, day)
    attempt = {"provider": "baostock", "result": "ok",
               "attempted_at": "2026-10-25T17:30:00Z", "elapsed_ms": 11.0,
               "reason": "baostock_fetch_timing", "login_ms": 2.0,
               "query_ms": 8.0, "error_code": "0"}
    runner = JobRunner(artifact_root=tmp_path, budget=RequestBudget(min_interval_seconds=0))
    result = runner.run("daily_kline", day,
                        fetch=lambda _: {"rows": rows, "source": "baostock", "provider": "baostock",
                                         "provider_attempts": [attempt]},
                        source="baostock>wencai>q2_final", now="2026-10-26T10:00:00+08:00")

    assert result.manifest["provider"] == "baostock"
    assert result.manifest["source"] == "baostock"
    assert result.manifest["provider_chain"] == "baostock>wencai>q2_final"
    assert "/source=baostock/" in result.manifest["manifest_uri"]
    persisted = result.manifest["provider_attempts"][0]
    assert {"provider", "result", "attempted_at", "elapsed_ms", "reason"} <= set(persisted)
    assert persisted["attempted_at"] == "2026-10-26T01:30:00+08:00"
    assert persisted["login_ms"] == 2.0 and persisted["query_ms"] == 8.0
    assert result.manifest["business_status"] == "FORMAL"


def test_legacy_composite_source_partition_remains_readable(tmp_path):
    from engine_core.data_pipeline.reader import load_dataset

    day = "2026-10-23"
    _, rows = _artifact("c522/daily_kline/2026-10-23/formal")
    catalog = ArtifactCatalog(tmp_path)
    _install_universe(catalog, day)
    committed = catalog.commit("daily_kline", day, rows,
                               {"dataset": "daily_kline", "trade_date": day,
                                "provider": "baostock", "provider_attempts": [],
                                "field_completeness": {field: "COMPLETE" for field in FIELDS}},
                               source="baostock>wencai>q2_final")

    assert "/source=baostock>wencai>q2_final/" in committed["manifest_uri"]
    loaded = load_dataset("daily_kline", day, prefer="file", catalog=catalog)
    assert loaded is not None and len(loaded.rows) == len(rows)
