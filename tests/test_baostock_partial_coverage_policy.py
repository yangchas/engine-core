from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from engine_core.data_pipeline.baostock_completion import (
    classify_missing_symbols,
    evaluate_baostock_completion,
)
from engine_core.data_pipeline.jobs import JobRunner
from engine_core.data_pipeline.providers.baostock_daily_provider import BaostockDailyProvider
from engine_core.data_pipeline.storage import ArtifactCatalog


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "real" / "core_artifacts" / "c594"
DAY = "2026-10-28"


def _policy():
    schedule = json.loads((ROOT / "src" / "engine_core" / "guardian" / "schedule.json").read_text())
    events = [event for event in schedule["events"]
              if event.get("event_id") in {"baostock_probe_1730", "baostock_retry_1740",
                                             "baostock_retry_1830", "baostock_retry_2000",
                                             "postmarket_publish"}]
    assert len(events) == 5
    policies = [event.get("args", {}).get("daily_kline_policy") for event in events]
    assert all(item == {"formal_coverage_threshold": 0.995, "source_empty_max": 20}
               for item in policies)
    assert all("TIER_PARTIAL" in event["args"].get("failure_classes", {}).get("retryable", {}).get("error_codes", [])
               for event in events if event.get("event_id", "").startswith("baostock_"))
    return policies[0]


def _rows(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _real_inputs(partition: str):
    universe = _rows(FIXTURES / "symbol_universe" / DAY / "baostock_query_all_stock" / "rows.jsonl.gz")
    active = {str(row["symbol"]).zfill(6) for row in universe
              if str(row.get("trade_status", "")).strip() != "0"}
    bars = _rows(FIXTURES / "daily_kline" / DAY / partition / "rows.jsonl.gz")
    bar_symbols = {str(row["symbol"]).zfill(6) for row in bars} & active
    source = json.loads((ROOT / "tests" / "fixtures" / "real" /
                         "baostock_query_all_stock_2026-10-28_2301.json").read_text(encoding="utf-8"))
    code_index = source["fields"].index("code")
    source_symbols = {str(row[code_index]).rsplit(".", 1)[-1] for row in source["rows"]}
    return active, bar_symbols, source_symbols, source


@pytest.mark.parametrize("partition,minimum_rows", [("coverage_0668", 3000), ("coverage_0986", 5000)])
def test_real_baostock_partitions_remain_retryable_when_missing_is_not_formal(partition, minimum_rows):
    active, bars, source_symbols, _ = _real_inputs(partition)
    assert len(bars) >= minimum_rows
    missing = classify_missing_symbols(active, bars, source_symbols)
    policy = _policy()
    result = evaluate_baostock_completion(
        active, bars, missing, coverage_threshold=policy["formal_coverage_threshold"],
        max_source_empty=policy["source_empty_max"])

    assert result["quality"]["known_missing"] == missing
    assert result["quality"]["coverage_threshold"] == 0.995
    assert result["business_status"] == "PARTIAL"
    assert result["retryable"] is True
    assert result["error_code"] == "TIER_PARTIAL"
    assert result["exit_code"] == 30
    assert result["alert_required"] is True

    rows = _rows(FIXTURES / "daily_kline" / DAY / partition / "rows.jsonl.gz")
    job = JobRunner(artifact_root=ROOT / "tests" / "tmp-c594-dry-run").run(
        "daily_kline", DAY,
        fetch=lambda _day: {"rows": rows, "expected_count": len(active),
                            "expected_symbols": sorted(active),
                            "source_symbols": sorted(source_symbols), "provider": "baostock",
                            "source": "baostock"},
        source="baostock", dry_run=True, baostock_policy=policy)
    assert job.manifest["quality"]["known_missing"] == missing
    assert job.manifest["business_status"] == "PARTIAL"
    assert job.manifest["completion_policy"] == policy


def test_real_20261028_latest_missing_classification_is_not_silently_weakened():
    active, bars, source_symbols, source = _real_inputs("coverage_0986")
    missing = classify_missing_symbols(active, bars, source_symbols)
    counts = {category: sum(value == category for value in missing.values())
              for category in ("SOURCE_UNAVAILABLE", "SOURCE_EMPTY", "UNEXPECTED")}

    assert counts == {"SOURCE_UNAVAILABLE": 1, "SOURCE_EMPTY": 73, "UNEXPECTED": 0}
    code_index = source["fields"].index("code")
    status_index = source["fields"].index("tradeStatus")
    empty_rows = {str(row[code_index]).rsplit(".", 1)[-1]: row[status_index]
                  for row in source["rows"]}
    assert all(empty_rows[symbol] == "1" for symbol, category in missing.items()
               if category == "SOURCE_EMPTY")

    policy = _policy()
    result = evaluate_baostock_completion(
        active, bars, missing, coverage_threshold=policy["formal_coverage_threshold"],
        max_source_empty=policy["source_empty_max"])
    assert result["quality"]["coverage"] < 0.995
    assert result["quality"]["source_empty_count"] == 73
    assert result["business_status"] == "PARTIAL"
    assert result["alert_required"] is True
    unavailable = {symbol: category for symbol, category in missing.items()
                   if category == "SOURCE_UNAVAILABLE"}
    unavailable_result = evaluate_baostock_completion(
        set(unavailable), set(), unavailable, coverage_threshold=0.995, max_source_empty=20)
    assert unavailable_result["business_status"] == "FORMAL"
    assert unavailable_result["alert_required"] is False
    loosened_threshold = evaluate_baostock_completion(
        active, bars, missing, coverage_threshold=0.98, max_source_empty=20)
    assert loosened_threshold["business_status"] == "PARTIAL"  # 73 SOURCE_EMPTY still exceed the configured cap.


def test_baostock_all_stock_uses_real_response_and_day_checkpoint_once(tmp_path):
    raw = json.loads((ROOT / "tests" / "fixtures" / "real" /
                      "baostock_query_all_stock_2026-10-28_2301.json").read_text(encoding="utf-8"))

    class Response:
        fields = raw["fields"]

        def __init__(self):
            self.index = -1

        def next(self):
            self.index += 1
            return self.index < len(raw["rows"])

        def get_row_data(self):
            return raw["rows"][self.index]

    class Module:
        def __init__(self):
            self.days = []

        def query_all_stock(self, *, day):
            self.days.append(day)
            return Response()

    module = Module()
    provider = BaostockDailyProvider(
        client_factory=lambda: None, symbols=[],
        source_universe_checkpoint=tmp_path / "source-universe.json",
        timeout_call=lambda fn, *args: fn(*args))
    provider._timing = {"query_count": 0, "query_ms": 0}
    first, first_error = provider._source_universe(type("Client", (), {"module": module})(), DAY)
    second, second_error = provider._source_universe(type("Client", (), {"module": module})(), DAY)
    code_index = raw["fields"].index("code")
    expected = sorted({str(row[code_index]).rsplit(".", 1)[-1] for row in raw["rows"]
                       if str(row[code_index]).rsplit(".", 1)[-1].isdigit() and
                       len(str(row[code_index]).rsplit(".", 1)[-1]) == 6})
    assert first_error is None and second_error is None
    assert first == second == expected
    assert module.days == [DAY]


def test_completion_policy_rejects_missing_or_mutated_categories():
    active, bars, source_symbols, _ = _real_inputs("coverage_0986")
    missing = classify_missing_symbols(active, bars, source_symbols)

    with pytest.raises(ValueError, match="known_missing_incomplete"):
        evaluate_baostock_completion(active, bars, dict(list(missing.items())[1:]),
                                     coverage_threshold=0.995, max_source_empty=20)

    wrong_source = source_symbols - {symbol for symbol, category in missing.items()
                                    if category == "SOURCE_EMPTY"}
    misclassified = classify_missing_symbols(active, bars, wrong_source)
    assert misclassified != missing
    assert misclassified["301139"] == "SOURCE_UNAVAILABLE"
    assert sum(value == "SOURCE_EMPTY" for value in misclassified.values()) == 0


def test_real_latest_partition_persists_policy_through_catalog_recalculation(tmp_path):
    active, bars, source_symbols, _ = _real_inputs("coverage_0986")
    universe = _rows(FIXTURES / "symbol_universe" / DAY / "baostock_query_all_stock" / "rows.jsonl.gz")
    daily_rows = _rows(FIXTURES / "daily_kline" / DAY / "coverage_0986" / "rows.jsonl.gz")
    policy = _policy()
    catalog = ArtifactCatalog(tmp_path)
    catalog.commit("symbol_universe", DAY, universe,
                   {"trade_date": DAY, "as_of": DAY, "source_asof": DAY,
                    "available_at": DAY + "T23:00:00+08:00"}, source="baostock")
    job = JobRunner(artifact_root=tmp_path).run(
        "daily_kline", DAY,
        fetch=lambda _day: {"rows": daily_rows, "expected_count": len(active),
                            "expected_symbols": sorted(active), "source_symbols": sorted(source_symbols),
                            "provider": "baostock", "source": "baostock"},
        source="baostock", baostock_policy=policy)
    quality = job.manifest["quality"]
    assert quality["known_missing"] == classify_missing_symbols(active, bars, source_symbols)
    assert quality["source_unavailable_count"] == 1
    assert quality["source_empty_count"] == 73
    assert job.manifest["business_status"] == "PARTIAL"
    assert quality["coverage"] < policy["formal_coverage_threshold"]


def test_known_source_unavailable_symbol_does_not_fail_td_watermark_check():
    active, bars, source_symbols, _ = _real_inputs("coverage_0986")
    missing = classify_missing_symbols(active, bars, source_symbols)
    symbol = next(symbol for symbol, category in missing.items()
                  if category == "SOURCE_UNAVAILABLE")

    class Store:
        def schema_exists(self):
            return True

        def day_counts(self, dates):
            return {DAY: 5150}

        def read_versions(self, day):
            return [{"version": "real-fixture", "provider": "baostock", "row_count": 5150,
                     "business_status": "FORMAL", "needs_upgrade": False}]

    from engine_core.guardian.daily_kline_coverage import INCOMPLETE, OK, TdCoverageSource

    source = TdCoverageSource(Store())
    watermarks = {symbol: "2026-10-27"}
    assert source.coverage([DAY], watermarks)[DAY].state == INCOMPLETE
    assert source.coverage([DAY], watermarks, missing)[DAY].state == OK
