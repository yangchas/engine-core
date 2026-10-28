from __future__ import annotations

import gzip
import json
from pathlib import Path

from engine_core.data_pipeline.dataset_contract import check_partition
from engine_core.data_pipeline.jobs import JobRunner
from engine_core.data_pipeline.limit_pool import kaipan_row
from engine_core.data_pipeline.limit_reasons import build_limit_reasons


ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-10-23"


def _source_rows():
    payload = json.loads((ROOT / "tests/fixtures/real/kaipan_pool_2026-10-23_L1_0537.json")
                         .read_text(encoding="utf-8"))
    raw = [row for page in payload["info"] if isinstance(page, list)
           for row in page if isinstance(row, list) and len(row) >= 23]
    pool = [row for row in (kaipan_row(record, 1, DAY) for record in raw) if row]
    symbol = next(row["symbol"] for row in pool if row["symbol"] == "002982")
    row = next(row for row in pool if row["symbol"] == symbol)
    registry_path = ROOT / "tests/fixtures/real/core_artifacts/c594/plate_registry/2026-10-13/current/rows.jsonl.gz"
    with gzip.open(registry_path, "rt", encoding="utf-8") as stream:
        registry = [{"plate_code": json.loads(line).get("plate_code")} for line in stream]
    return build_limit_reasons(DAY, [row], plate_registry_rows=registry,
                               plate_registry_loaded=True, pool_source="kaipan_getHisBans")


def _write_contract_partition(root: Path, *, future_event_date: bool = False,
                               provider: str = "kaipan_getHisBans"):
    built = _source_rows()
    rows = [dict(row) for row in built["rows"]]
    if future_event_date:
        rows[0]["event_date"] = "2026-10-24"
    fetch_result = {
        **built,
        "rows": rows,
        "provider": provider,
        "provider_chain": built["provider_chain"],
        "provider_attempts": [{"provider": provider, "result": "READY",
                               "elapsed_ms": 0, "reason": "captured_pool_row", "row_count": len(rows)}],
        "quality_status": "READY",
        "business_status": "FORMAL",
        "field_completeness": {name: sum(row.get(name) not in (None, "") for row in rows)
                               for name in ("reason", "main_reason", "plate_code")},
        "partial": False,
    }
    JobRunner(artifact_root=root).run(
        "limit_reasons", DAY, fetch=lambda _day: fetch_result,
        source=provider, as_of=DAY + "T15:00:00+08:00",
    )


def test_real_limit_reasons_partition_passes_executable_contract(tmp_path):
    _write_contract_partition(tmp_path)
    report = check_partition(tmp_path, "limit_reasons", DAY)

    assert report.passed, report.summary()


def test_per_symbol_full_history_is_a_formal_kaipan_primary_provider(tmp_path):
    _write_contract_partition(tmp_path, provider="kaipan_getBanReasons(st=200)")
    report = check_partition(tmp_path, "limit_reasons", DAY)

    assert report.passed, report.summary()


def test_contract_rejects_reason_date_after_described_trade_date(tmp_path):
    _write_contract_partition(tmp_path, future_event_date=True)
    report = check_partition(tmp_path, "limit_reasons", DAY)

    failures = [item for item in report.checks if item["status"] == "FAIL"]
    assert any(item["id"] == "B2c_event_date" for item in failures)
